"""管理 Dog 和 UT3200 的电脑端采集线程、停止事件与通信句柄。"""

import threading
import HAL_ut3200_serial
import HAL_udp_receive

# 各设备的线程记录保留到对应停止函数完成清理。
thread_ut3200 = None
thread_dog = None
stop_event = threading.Event()          # 旧 BLL 命令行和调试流程等待的系统事件
ut3200_stop_event = threading.Event()   # UT3200 采集线程的停止通知
dog_stop_event = threading.Event()      # Dog 接收线程的停止通知
ser = None                       # UT3200 的 serial.Serial 串口句柄；None 表示当前未持有
sock = None                      # 机器狗的 IPv4 UDP socket 句柄；None 表示当前未持有

def HAL_WaitForStop(timeout=0.2):
    """供旧 BLL 调试流程等待系统事件。收到通知返回 True，超时返回 False。"""
    return stop_event.wait(timeout)

def HAL_HasResources():
    """检查是否还有设备句柄或待清理的线程记录。"""
    return any(
        resource is not None
        for resource in (sock, ser, thread_dog, thread_ut3200)
    )

def HAL_Dog_Start(snapshot,data_write_callback):
    """
    绑定 UDP socket 并启动 Dog 接收线程。
    snapshot：BLL_GetDevicesSnapshot 返回的字典，含 devices 列表及各设备的 signals 列表。
    data_write_callback：BLL 传入的写入函数对象，例如 BLL_DataWrite，而非调用结果。
        调用形式为 data_write_callback(updates, timestamp)，返回值不使用。
        updates 是三元组列表 [(设备下标, 该设备内的信号下标, 实际值), ...]；
        两个下标均从 0 开始，值由串口传 float、UDP 传 str。
        timestamp 是采集线程记录的本批数据的本地时间，精确到毫秒。
        两个采集线程可同时调用该函数，因此回调需要保证线程安全。
    正常返回表示启动流程完成，失败则抛出异常。
    """
    global sock,thread_dog
    # 先确认 Dog 上一轮的线程和 socket 已清理。
    if thread_dog is not None or sock is not None:
        raise RuntimeError("已有采集任务运行，请先停止")
    # 清除 Dog 的停止通知，准备本次接收。
    dog_stop_event.clear()

    try:
        # sock 为已绑定的 UDP 句柄；dog_routes 的三项为（设备下标、信号下标、报文字段下标）。
        # 三项均从 0 开始；停用信号仍占据报文字段位置，不会使后续字段前移。
        sock, dog_routes = HAL_udp_receive.HAL_Dog_Open(snapshot, host="0.0.0.0", port=8080, timeout=0.2)
        print("[HAL] UDP 初始化成功，绑定 %s:%d" % sock.getsockname())
        thread_dog = threading.Thread(
        target=HAL_udp_receive.HAL_Dog_ReceiveData_Thread,
        args=(sock, dog_stop_event, dog_routes, data_write_callback),
        name="dog_collect",
        daemon=True)
        thread_dog.start()
    except Exception as start_error:
        try:
            HAL_Dog_Stop()
        except Exception as cleanup_error:
            # 把清理失败附在启动异常上，保留最初的故障原因。
            start_error.add_note(f"Dog 清理失败：{cleanup_error}")
        raise

def HAL_UT3200_Start(snapshot,data_write_callback):
    global ser,thread_ut3200
    # 先确认 UT3200 上一轮的线程和串口已清理。
    if thread_ut3200 is not None or ser is not None:
        raise RuntimeError("已有采集任务运行，请先停止")
    # 清除 UT3200 的停止通知，准备本次采集。
    ut3200_stop_event.clear()
    try:
        # ser 为已打开的串口句柄；ut3200_routes 为 list[tuple[int, int, int]]。
        # 每项依次为（设备下标、该设备内的信号下标、温度数组下标），均从 0 开始；
        # 温度数组下标由配置中的仪器通道号 ch - 1 得到。
        ser,ut3200_routes = HAL_ut3200_serial.HAL_UT3200_InitDevice(snapshot)
        print("[HAL] UT3200 初始化成功")
        thread_ut3200 = threading.Thread(
            target=HAL_ut3200_serial.HAL_UT3200_CollectTempData_Thread,
            args=(ser,ut3200_stop_event,ut3200_routes,data_write_callback),
            name="ut3200_collect",
            daemon=True )
        thread_ut3200.start()
    except Exception as start_error:
        try:
            HAL_UT3200_Stop()
        except Exception as cleanup_error:
            # 把清理失败附在启动异常上，保留最初的故障原因。
            start_error.add_note(f"UT3200 清理失败：{cleanup_error}")
        raise

def HAL_Shutdown(thread,stop_event,handle):
    """
    通知指定的采集线程退出，等待结束后关闭电脑端的通信句柄。
    等待超时或关闭句柄失败时抛出异常，由调用方保留资源记录。
    """
    stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=3)
        if thread.is_alive():
            raise RuntimeError(f"{thread.name} 退出超时")

    if handle is not None:
        handle.close()

def HAL_UT3200_Stop():
    global thread_ut3200,ser
    HAL_Shutdown(thread_ut3200, ut3200_stop_event, ser)
    thread_ut3200 =None
    ser = None

def HAL_Dog_Stop():
    global thread_dog,sock
    HAL_Shutdown(thread_dog, dog_stop_event, sock)
    thread_dog = None
    sock = None





