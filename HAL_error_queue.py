from datetime import datetime
from queue import Empty, SimpleQueue

_errors = SimpleQueue()

def report_device_error(device, error):
    """由设备采集线程调用，保留设备名、异常类型和原因。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail = " ".join(str(error).split())
    message = f"{timestamp}  {device} 采集异常：{type(error).__name__}: {detail}"
    _errors.put(message)
    print(message)

def take_device_errors():
    """由 UI 调用，一次取走当前所有消息。队列为空时立即返回。"""
    messages = []
    while True:
        try:
            messages.append(_errors.get_nowait())
        except Empty:
            return messages