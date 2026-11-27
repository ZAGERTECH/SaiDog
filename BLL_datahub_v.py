# 管理设备配置、采集数据和 HAL 启停。

import csv
import json
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from math import isfinite
from readerwriterlock import rwlock

import HAL_startup_shutdown

# ============================================================
# 一、数据结构定义（字段显式声明）
# ============================================================

# 新增 JSON 字段时，在 signals_config.json 和对应的 dataclass 中同步声明。
# 信号级字段（如 position）加到 Signal，加载和快照会自动读取已声明的信号字段。
# 设备级字段加到 Device，还要在 BLL_JSON_LoadDevices 构造 Device 时读取。
# 界面要展示新增字段时，需在 UI 中增加对应列。

@dataclass
class Device:
    id: str  # 必填，设备间唯一，供 HAL 匹配设备
    name: str = ""  # 设备展示名
    api: str = ""  # 采集函数名
    signals: list["Signal"] = field(default_factory=list)  # 按 JSON 顺序保存


@dataclass
class Signal:
    # 设备名称和采集函数名从所属的 Device 读取。
    id: str  # 必填，全项目唯一
    enabled: bool | None = None  # None=待定 / True=启用 / False=停用
    ch: int | None = None  # UT3200 通道号，从 1 开始
    name: str = ""  # 信号展示名
    unit: str = ""
    min: float | None = None  # 量程下限
    max: float | None = None  # 量程上限
    fmt: str = ""  # 显示格式，如 "{:.1f}"
    value: float | str | None = None  # 最新采集值，由 HAL 回调写入
    ts: str = ""  # 采集端本地时间，格式为 YYYY-MM-DD HH:MM:SS.sss


# ============================================================
# 二、全局变量：初始化完成后留在内存里的状态
# ============================================================

SIGNALS_CONFIG_PATH = "signals_config.json"  # 命令行入口使用，路径相对于当前工作目录

# 从 dataclass 提取字段名，用于校验 JSON 中的设备字段和信号字段。
_SIGNAL_FIELDS = {f.name for f in fields(Signal)}
_DEVICE_FIELDS = {f.name for f in fields(Device)}

_DEVICES = []  # list[Device]，采集回调按设备及其 signals 的下标定位
_DEBUG_THREAD = None  # 调试打印线程句柄

# 多个读者可同时取快照，写入与其他读写互斥。写者排队时，新读者等待。
# 锁内只做内存操作，文件、串口和网络 I/O 放在锁外。
_RWLOCK_DEVICE = rwlock.RWLockWrite()


# ============================================================
# 三、JSON 相关：加载配置 / 初始化 / 取快照/
# ============================================================

def BLL_JSON_LoadDevices(json_path):
    """从 json_path 加载设备配置，返回 list[Device]，保留设备和信号的配置顺序。

    配置按 {"devices": [{..., "signals": [...]}]} 分组。
    缺少 devices、设备或信号缺少 ID、ID 重复或出现未声明字段时，抛出 ValueError。
    停用信号仍在原来的位置。HAL 生成路由时跳过 enabled=False 的信号。
    """
    with open(json_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if "devices" not in cfg:
        raise ValueError("配置缺少 devices 数组（只支持设备分组写法）: " + json_path)

    devices = []
    seen_dev = set()
    seen_sig = set()
    for dev in cfg["devices"]:
        did = dev.get("id")
        if not did:
            raise ValueError("有一个设备缺少 id 字段")
        if did in seen_dev:
            raise ValueError("设备 id 重复: " + repr(did))
        seen_dev.add(did)
        for k in dev:
            if k not in _DEVICE_FIELDS and k != "signals":
                raise ValueError("设备 %r 的字段 %r 在 Device 类里没有定义，"
                                 "请在 Device 里加一行（例如 %s: str = \"\"）"
                                 % (did, k, k))
        d = Device(id=did, name=dev.get("name", ""), api=dev.get("api", ""))

        for sig in dev.get("signals", []):
            sid = sig.get("id")
            if not sid:
                raise ValueError("设备 %s 下有一条信号缺少 id 字段" % d.id)
            if sid in seen_sig:
                raise ValueError("信号 id 重复: " + repr(sid))
            seen_sig.add(sid)

            s = Signal(id=sid)
            for k, v in sig.items():
                if k == "id":
                    continue
                if k not in _SIGNAL_FIELDS:
                    raise ValueError("信号 %r 的字段 %r 在 Signal 类里没有定义，"
                                     "请在 Signal 里加一行（例如 %s: str = \"\"）"
                                     % (sid, k, k))
                setattr(s, k, v)
            d.signals.append(s)
        devices.append(d)

    return devices


def BLL_JSON_InitDevices(json_path):
    """在 HAL 资源关闭后加载配置并替换全局设备列表。

    采集线程持有按当前配置生成的路由，重新初始化前须先停止采集并释放资源。
    """
    if HAL_startup_shutdown.HAL_HasResources():
        raise RuntimeError("HAL 尚未关闭，不能重新初始化 DataHub")

    # 在锁外读取并校验文件，成功后再持写锁一次性替换设备列表。
    devices = BLL_JSON_LoadDevices(json_path)
    global _DEVICES
    with _RWLOCK_DEVICE.gen_wlock():
        _DEVICES = devices


def BLL_GetDevicesSnapshot():
    """返回可独立修改的字典快照，供 UI 读取和 HAL 生成路由。

    结构为 {"devices": [{"id": ..., "name": ..., "api": ..., "signals": [...]}]}。
    每条信号以包含 Signal 全部字段的字典表示。设备和信号顺序与 _DEVICES 一致，
    采集期间须保持这个顺序，才能让 HAL 的下标继续指向同一条信号。
    """
    with _RWLOCK_DEVICE.gen_rlock():
        if not _DEVICES:
            raise RuntimeError("未初始化，请先调用 BLL_JSON_InitDevices(配置文件路径)")
        return {"devices": [asdict(device) for device in _DEVICES]}


# ============================================================
# 四、数据写入：HAL 采集值由此传入/ 判断温度是否超出上下限
# ============================================================

def BLL_DataWrite(updates, timestamp):
    """将 HAL 的一批采集值写入 DataHub。

    updates 的每项为 (设备下标, 信号下标, 采集值)。两个下标从 0 开始，
    分别索引 _DEVICES 和该设备的 signals。调用方须保证三元组格式和下标有效。
    例如 [(1, 0, 25.6)] 将第 2 个设备的第 1 条信号写为 25.6。

    串口传 float，UDP 传原始字段 str，也允许传 None。采集值原样保存。
    每批数据共用同一个 timestamp，即 HAL 记录的毫秒级本地采集时间。
    空列表直接返回。重复目标以后一次写入为准，中途出错时已写入的项会保留。
    """
    if not updates:
        return
    with _RWLOCK_DEVICE.gen_wlock():
        for device_index, signal_index, value in updates:
            signal = _DEVICES[device_index].signals[signal_index]
            # 在同一把写锁下更新值和时间，保证快照中的两者对应。
            signal.value = value
            signal.ts = timestamp

def BLL_CheckTemperatureSnapshot(snapshot, previous_states):
    states = previous_states.copy()
    messages = []
    for device in snapshot["devices"]:
        for signal in device["signals"]:
            if signal["enabled"] is False or signal["unit"] != "°C":
                continue

            try:
                value = float(signal["value"])
            except (TypeError, ValueError, OverflowError):
                continue
            if not isfinite(value):
                continue

            signal_id = signal["id"]
            low, high = signal["min"], signal["max"]
            if low is not None and value < low:
                state,limit = "超出下限",low
            elif high is not None and value > high:
                state,limit= "超出上线",high
            else:
                states.pop(signal_id,None)
                continue

            if previous_states.get(signal_id) != state :
                messages.append(
                    f'{signal["ts"]}  温度报警 · {signal["name"]} '
                    f'{value:g} °C，{state} {limit:g} °C'
                )
            states[signal_id] = state
    return  states, messages


# ============================================================
# 五、CSV 存储（10Hz）
# ============================================================

# 每 0.1 秒（10Hz）把各启用信号的「最新值 + 采集时间」写一行到 csv。
# UI 按下「开始存储」调用 BLL_CSV_Start，每次新建一个「年-月-日-时-分-秒.csv」；
# 按下「停止存储」调用 BLL_CSV_Stop 关闭文件。文件名位于程序当前工作目录。

_CSV_THREAD = None              # CSV 存储线程句柄
_CSV_STOP_EVENT = threading.Event()  # 存储线程的停止通知
_CSV_FILE = None                # 当前打开的 csv 文件句柄
_CSV_WRITER = None              # csv 写入器
_CSV_INTERVAL = 0.1             # 存储周期（秒），10Hz


def _BLL_CSV_CollectSignals():
    """返回需要存储的信号列表，跳过 enabled=False 的信号（与 HAL 路由一致）。"""
    with _RWLOCK_DEVICE.gen_rlock():
        return [
            signal
            for device in _DEVICES
            for signal in device.signals
            if signal.enabled is not False
        ]


def _BLL_CSV_SignalLabel(signal):
    """返回信号在表头里的列名，例如「左前腿髋关节电流（A）」。"""
    if signal.unit:
        return f"{signal.name}（{signal.unit}）"
    return signal.name


def _BLL_CSV_Header(signals):
    """表头：每个信号占两列，依次为「名称（单位）,时间」。"""
    header = []
    for signal in signals:
        header.append(_BLL_CSV_SignalLabel(signal))
        header.append("时间")
    return header


def _BLL_CSV_CollectRow(signals):
    """在读锁内读取一行数据：每个信号的「值,采集时间」。"""
    row = []
    with _RWLOCK_DEVICE.gen_rlock():
        for signal in signals:
            row.append(signal.value if signal.value is not None else "None")
            row.append(signal.ts)
    return row

def _BLL_CSV_Store_Thread():
    """每 0.1 秒读取一次信号并写入 csv，收到停止通知后退出并关闭文件。"""
    try:
        signals = _BLL_CSV_CollectSignals()
        while not _CSV_STOP_EVENT.wait(_CSV_INTERVAL):
            _CSV_WRITER.writerow(_BLL_CSV_CollectRow(signals))
            _CSV_FILE.flush()  # 立即写盘，避免意外退出丢失数据
    finally:
        if _CSV_FILE is not None:
            _CSV_FILE.close()


def BLL_CSV_Start():
    """开始 CSV 存储：新建「年-月-日-时-分-秒.csv」并启动存储线程。

    成功返回文件名；已有存储在进行中时抛出 RuntimeError。
    """
    global _CSV_THREAD, _CSV_FILE, _CSV_WRITER
    if _CSV_THREAD is not None and _CSV_THREAD.is_alive():
        raise RuntimeError("CSV 存储已在进行中，请先停止")

    signals = _BLL_CSV_CollectSignals()
    if not signals:
        raise RuntimeError("没有可存储的信号，请先初始化设备并启用信号")

    filename = datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".csv"
    # newline="" 避免 Windows 下出现多余空行；utf-8-sig 让 Excel 打开中文不乱码。
    _CSV_FILE = open(filename, "w", newline="", encoding="utf-8-sig")
    _CSV_WRITER = csv.writer(_CSV_FILE)
    _CSV_WRITER.writerow(_BLL_CSV_Header(signals))

    _CSV_STOP_EVENT.clear()
    _CSV_THREAD = threading.Thread(
        target=_BLL_CSV_Store_Thread,
        name="bll_csv_store",
        daemon=True,
    )
    _CSV_THREAD.start()
    return filename


def BLL_CSV_Stop():
    """停止 CSV 存储，等待存储线程退出并关闭文件。"""
    global _CSV_THREAD, _CSV_FILE, _CSV_WRITER
    _CSV_STOP_EVENT.set()
    if _CSV_THREAD is not None and _CSV_THREAD.is_alive():
        _CSV_THREAD.join(timeout=3)
    _CSV_THREAD = None
    _CSV_FILE = None
    _CSV_WRITER = None


# ============================================================
# 六、数据超时清空（看门狗，5 秒循环）
# ============================================================

# 设备打开后启动：每 5 秒检查一次，把超过阈值仍未更新的信号 value 清空为 None，
# 让界面能及时反映「设备停采 / 失联」的状态。判断依据是信号的时间戳 ts。

_WATCHDOG_THREAD = None              # 看门狗线程句柄
_WATCHDOG_STOP_EVENT = threading.Event()  # 看门狗线程的停止通知
_WATCHDOG_INTERVAL = 5.0             # 循环周期（秒）：每 5 秒检查一次
_WATCHDOG_TIMEOUT = 5.0              # 超时阈值（秒）：超过这个时间没更新就清空


def _BLL_ParseTs(ts):
    """把 ts 字符串解析成 datetime；空串或格式不对返回 None。"""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def _BLL_Watchdog_Thread():
    """每 5 秒检查一次，把超过阈值仍未更新的信号 value 清空为 None。"""
    while not _WATCHDOG_STOP_EVENT.wait(_WATCHDOG_INTERVAL):
        now = datetime.now()
        with _RWLOCK_DEVICE.gen_wlock():
            for device in _DEVICES:
                for signal in device.signals:
                    if signal.value is None:
                        continue
                    last = _BLL_ParseTs(signal.ts)
                    if last is None:
                        continue
                    if (now - last).total_seconds() > _WATCHDOG_TIMEOUT:
                        signal.value = None
                        signal.ts = ""


def BLL_Watchdog_Start():
    """启动数据超时清空线程；已在运行则忽略（不重复启动）。"""
    global _WATCHDOG_THREAD
    if _WATCHDOG_THREAD is not None and _WATCHDOG_THREAD.is_alive():
        return
    _WATCHDOG_STOP_EVENT.clear()
    _WATCHDOG_THREAD = threading.Thread(
        target=_BLL_Watchdog_Thread,
        name="bll_watchdog",
        daemon=True,
    )
    _WATCHDOG_THREAD.start()


def BLL_Watchdog_Stop():
    """停止数据超时清空线程并等待其退出。"""
    global _WATCHDOG_THREAD
    _WATCHDOG_STOP_EVENT.set()
    if _WATCHDOG_THREAD is not None and _WATCHDOG_THREAD.is_alive():
        _WATCHDOG_THREAD.join(timeout=3)
    _WATCHDOG_THREAD = None


# ============================================================
# 七、调试打印
# ============================================================

def BLL_Debug_PrintDevices_Thread():
    """每秒读取并打印一次设备快照，收到停止通知后退出。"""
    while not HAL_startup_shutdown.HAL_WaitForStop(1.0):
        # 先取独立快照，再序列化并打印，让输出留在锁外。
        snapshot = BLL_GetDevicesSnapshot()
        print(
            "[BLL] 设备数据：\n"
            + json.dumps(snapshot, ensure_ascii=False, indent=2)
        )


# ============================================================
# 八、线程编排（初始化 / 启动 / 停止）
# ============================================================

# BLL 初始化 DataHub 并调用各设备的启停接口。
# 采集线程、通信句柄和各设备的停止事件由 HAL 管理。

def BLL_All_InitSystem(json_path):
    """为命令行调试入口加载配置并启动打印线程。

    成功返回 True，初始化或线程启动抛出普通异常时返回 False。
    """
    global _DEBUG_THREAD
    try:
        BLL_JSON_InitDevices(json_path)
        print("[BLL] JSON 初始化成功")
        try:
            print("[BLL] HAL 启动成功")
            _DEBUG_THREAD = threading.Thread(
                target=BLL_Debug_PrintDevices_Thread,
                name="bll_debug_print",
                daemon=True,
            )
            _DEBUG_THREAD.start()
        except BaseException:
            raise

        return True

    except Exception as error:
        print("[BLL] 系统初始化失败：%s" % error)
        return False


# UI 先初始化 DataHub。启动时给 HAL 传入用于生成路由的快照及写入回调，
# 采集线程收到一批数据后调用 BLL_DataWrite(updates, timestamp)。
def BLL_StartDog():
    snapshot = BLL_GetDevicesSnapshot()
    HAL_startup_shutdown.HAL_Dog_Start(snapshot, BLL_DataWrite)
    BLL_Watchdog_Start()  # 设备成功打开后，启动数据超时清空


def BLL_StartUT3200():
    snapshot = BLL_GetDevicesSnapshot()
    HAL_startup_shutdown.HAL_UT3200_Start(snapshot, BLL_DataWrite)
    BLL_Watchdog_Start()  # 设备成功打开后，启动数据超时清空


def BLL_StopDog():
    HAL_startup_shutdown.HAL_Dog_Stop()


def BLL_StopUT3200():
    HAL_startup_shutdown.HAL_UT3200_Stop()

def BLL_ShutDowmAll():
    HAL_startup_shutdown.HAL_RequestDeviceStops()
    _CSV_STOP_EVENT.set()
    _WATCHDOG_STOP_EVENT.set()

    errors = []
    for name, stop in (
            ("UDP", BLL_StopDog),
            ("UT3200", BLL_StopUT3200),
            ("CSV", BLL_CSV_Stop),
            ("看门狗", BLL_Watchdog_Stop),
    ):
        try:
            # 执行已有的停止流程，等待线程并关闭资源。
            stop()
        except Exception as error:
            errors.append(f'{name}: {error}')

    if errors:
        raise  RecursionError("\n".join(errors))


def BLL_RoboDogDetect(json_path):
    """运行命令行调试入口，加载配置并等待 HAL 系统停止通知或 Ctrl+C。"""
    if HAL_startup_shutdown.HAL_HasResources():
        print("[BLL] 系统已启动或上次关闭未完成，拒绝重复启动")
        return
    try:
        if not BLL_All_InitSystem(json_path):
            print("[BLL] 初始化失败，程序退出")
            return
        print("\n程序运行中，按 Ctrl+C 停止...")
        while not HAL_startup_shutdown.HAL_WaitForStop(0.2):
            pass
        print("[BLL] 收到 HAL 停止通知，正在关闭系统...")
    except KeyboardInterrupt:
        print("\n[BLL] 收到用户停止请求，正在关闭系统...")


def main():
    BLL_RoboDogDetect(SIGNALS_CONFIG_PATH)


if __name__ == "__main__":
    main()
