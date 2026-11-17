'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const stickers = require('./stickers');
const { readJson } = require('../infra/jsonFile');

/** 首次只为完全空的图库自动导入；已有图库由用户手动导入。 */
async function installPresets(config, root = config.root || process.cwd(), { manual = false } = {}) {
  if (!stickers.isOn(config)) return 0;
  const assets = path.join(root, 'assets', 'stickers');
  const manifestFile = path.join(assets, 'manifest.json');
  if (!fs.existsSync(manifestFile)) return 0;
  if (!stickers.regularFile(manifestFile, assets)) throw new Error('预设表情包清单不是安全的普通文件');
  const manifest = readJson(manifestFile);
  if (manifest?.version !== 1 || !Array.isArray(manifest.files) || !manifest.files.length || manifest.files.length > 500) {
    throw new Error('预设表情包清单格式无效');
  }
  const labels = Object.values(stickers.EMOTIONS);
  const seen = new Set();
  for (const item of manifest.files) {
    const parts = typeof item?.file === 'string' ? item.file.split('/') : [];
    if (parts.length !== 2 || !labels.includes(parts[0])
      || !/^whale-[a-z0-9-]+\.(png|gif|jpe?g|webp)$/.test(parts[1])
      || !/^[a-f0-9]{64}$/.test(item.sha256) || seen.has(item.file)) {
      throw new Error('预设表情包清单包含无效文件名或校验值');
    }
    seen.add(item.file);
  }

  const dir = stickers.stickerDir(config, root);
  const marker = path.join(dir, '.whale-presets-v1.json');
  let installed;
  try { installed = await fs.promises.lstat(marker); }
  catch (err) { if (err.code !== 'ENOENT') throw err; }
  if (installed) {
    if (!stickers.regularFile(marker, dir)) throw new Error('预设表情包初始化标记不是安全的普通文件');
    return 0;
  }

  stickers.safeDirectory(dir);
  // 扫整个图库，根目录散图、旧分类和自定义分类也算用户已有的内容。
  if (!manual && (await stickers.listImagesAsync(dir)).length > 0) return 0;
  let copied = 0;
  for (const item of manifest.files) {
    if (!stickers.isOn(config)) return copied;
    const source = path.join(assets, item.file);
    if (!stickers.regularFile(source, assets)) throw new Error('预设表情包文件不安全或不存在：' + item.file);
    const bytes = await fs.promises.readFile(source);
    const digest = crypto.createHash('sha256').update(bytes).digest('hex');
    const type = stickers.imageType(bytes);
    const ext = path.extname(item.file);
    if (digest !== item.sha256 || !type || type !== (ext === '.jpeg' ? '.jpg' : ext)) {
      throw new Error('预设表情包校验失败：' + item.file);
    }
    const target = path.join(dir, item.file);
    stickers.safeDirectory(path.dirname(target));
    try {
      await fs.promises.copyFile(source, target, fs.constants.COPYFILE_EXCL);
      copied++;
      stickers.invalidateScan(path.dirname(target));
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      // 同名文件归用户所有，内容不同也不能覆盖；链接则直接拒绝。
      if (!stickers.regularFile(target, dir)) throw new Error('预设表情包目标不是安全的普通文件：' + item.file);
    }
  }
  if (!stickers.isOn(config)) return copied;
  try {
    await fs.promises.writeFile(marker, JSON.stringify({ version: 1, files: manifest.files.map(item => item.file) }), { flag: 'wx' });
  } catch (err) { if (err.code !== 'EEXIST') throw err; }
  return copied;
}

module.exports = { installPresets };
