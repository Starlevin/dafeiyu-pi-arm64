'use strict';

/**
 * 启动器退出后内核必须跟着退出。
 *
 * 启动器把内核的 stdin 接在一条匿名管道上，自己从不写入。正常停止会先发
 * shutdown；但如果启动器崩溃、被任务管理器强杀，或者更新脚本直接杀进程，
 * 就只剩系统关闭管道。内核若还监听着端口，就会变成残留的 node 进程，
 * 下次启动还会因为端口被占而连到旧实例。这里把 stdin 关闭当成父进程已死。
 */
function watchParentPipe(stream, onClosed) {
  if (!stream || typeof stream.once !== 'function') return () => {};
  let fired = false;
  const fire = () => {
    if (fired) return;
    fired = true;
    onClosed();
  };
  // Windows 上父进程被杀时通常先 end；某些情况下只发 close，两个都接。
  stream.once('end', fire);
  stream.once('close', fire);
  return () => {
    stream.removeListener('end', fire);
    stream.removeListener('close', fire);
  };
}

module.exports = { watchParentPipe };
