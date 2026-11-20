# 管理设备配置、采集数据和 HAL 启停。

import json
import threading
from dataclasses import asdict, dataclass, field, fields

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
# 三、JSON 相关：加载配置 / 初始化 / 取快照
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
# 四、数据写入：HAL 采集值由此传入
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


# ============================================================
# 五、CSV 存储（10Hz）
# ============================================================

# 预留 CSV 存储实现。


# ============================================================
# 六、调试打印
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
# 七、线程编排（初始化 / 启动 / 停止）
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


def BLL_StartUT3200():
    snapshot = BLL_GetDevicesSnapshot()
    HAL_startup_shutdown.HAL_UT3200_Start(snapshot, BLL_DataWrite)


def BLL_StopDog():
    HAL_startup_shutdown.HAL_Dog_Stop()


def BLL_StopUT3200():
    HAL_startup_shutdown.HAL_UT3200_Stop()


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
