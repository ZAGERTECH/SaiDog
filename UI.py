"""机器狗监控台。主窗口负责布局、实时读数和采集操作的结果显示。"""
from datetime import datetime
from pathlib import Path
from threading import Thread
from traceback import format_exception_only
from HAL_error_queue import take_device_errors
from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
)

from BLL_datahub_v import (
    BLL_CSV_Start,
    BLL_CSV_Stop,
    BLL_GetDevicesSnapshot,
    BLL_JSON_InitDevices,
    BLL_StartDog,
    BLL_StartUT3200,
    BLL_StopDog,
    BLL_StopUT3200,
    BLL_ShutDowmAll,
    BLL_CheckTemperatureSnapshot,
)


class UI_MainWindow(QMainWindow):
    # 声明操作结果信号。后台操作完成后调用 emit()，由 UI 主线程的槽接收。
    # 参数依次为按钮、设备名、勾选状态、下一次操作的文字和异常。
    button_result_signal = Signal(object, str, bool, str, object)

    # ── 主窗口与区域布局 ──────────────────────────────────

    def __init__(self, snapshot):
        """按顶部按钮栏、中部卡片区、底部详情区的顺序组装窗口。"""
        super().__init__()
        self.setWindowTitle("机器狗监控台")
        self.resize(900, 600)

        # 按信号名称保存读数标签，配置应保证名称全局唯一。
        self.values_labels = {}
        #存储报警状态信息
        self.temperature_alarm_states = {}
        central = QWidget()
        main_layout = QVBoxLayout(central)
        #添加按钮行
        main_layout.addLayout(self._build_button_row())
        #添加实时监控卡片的滚动区
        main_layout.addWidget(self._build_card_scroll(snapshot))
        #添加传感器详情和热成像视频
        main_layout.addLayout(self._build_detail_panels(), 1)

        main_layout.addWidget(self.status())
        self.setCentralWidget(central)

        self.button_thread = None
        # 后台发出结果后，Qt 将接收槽排到 UI 主线程执行。
        self.button_result_signal.connect(
            self.update_button_after_operation,
            Qt.ConnectionType.QueuedConnection,
        )

        # 主线程每 100 毫秒读取一次快照。
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_readings)
        self.refresh_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.refresh_timer.start(100)


    def _build_button_row(self):
        """创建顶部按钮栏，并为各按钮绑定对应的 BLL 启停函数。"""
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.addStretch()

        self.udp_button = QPushButton()
        self.bind_button_click(self.udp_button, "UDP", BLL_StartDog, BLL_StopDog)

        self.ut3200_button = QPushButton()
        self.bind_button_click(
            self.ut3200_button, "UT3200", BLL_StartUT3200, BLL_StopUT3200
        )

        self.csv_button = QPushButton()
        self.bind_button_click(
            self.csv_button,
            "CSV",
            BLL_CSV_Start,
            BLL_CSV_Stop,
            text_start="开始存储",
            text_stop="结束存储",
        )

        # 热像仪接入后，在这里绑定它的启停函数。
        self.IRCam_button = QPushButton("IRCam开始采集")

        button_row.addWidget(self.udp_button)
        button_row.addWidget(self.ut3200_button)
        button_row.addWidget(self.IRCam_button)
        button_row.addStretch(1)
        button_row.addWidget(self.csv_button)
        return button_row

    def _build_card_scroll(self, snapshot):
        """把实时监控卡片装进滚动区，统一设置滚动和尺寸。"""
        monitor_page = self.build_monitor_page(snapshot)
        card_scroll = QScrollArea()
        card_scroll.setWidgetResizable(True)
        card_scroll.setWidget(monitor_page)
        card_scroll.setMaximumHeight(280)
        card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        card_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # 最小宽度包含卡片页、竖向滚动条和两侧边框。
        card_min_width = monitor_page.minimumSizeHint().width()
        scrollbar_width = card_scroll.verticalScrollBar().sizeHint().width()
        frame_width = 2 * card_scroll.frameWidth()
        card_scroll.setMinimumWidth(card_min_width + scrollbar_width + frame_width)
        return card_scroll

    def _build_detail_panels(self):
        """布置传感器详情和热成像视频，两个面板平分宽度。"""
        panel_row = QHBoxLayout()
        panel_row.setSpacing(12)

        sensor = QGroupBox("传感器详情")
        sensor_layout = QVBoxLayout(sensor)
        sensor_label = QLabel("待完成")
        sensor_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sensor_layout.addWidget(sensor_label)

        ircam = QGroupBox("热成像视频")
        ircam_layout = QVBoxLayout(ircam)
        self.ircam_label = QLabel("待完成")
        self.ircam_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ircam_layout.addWidget(self.ircam_label)

        panel_row.addWidget(sensor, 1)
        panel_row.addWidget(ircam, 1)
        return panel_row

    def status(self):
        status_log = QPlainTextEdit()
        #给屏幕阅读器取个名字
        status_log.setAccessibleName("设备状态")
        #无消息时的字段是"设备状态"
        status_log.setPlaceholderText("设备状态")
        status_log.setReadOnly(True)
        status_log.setMaximumBlockCount(200)  # 超过 200 条后移除最早的消息

        # 文档上方和下方各留 3 像素，再加上边框和两行文字的高度。
        margin = 3
        status_log.document().setDocumentMargin(margin)
        line_height = status_log.fontMetrics().lineSpacing()
        frame = status_log.frameWidth()
        ## 高度：文字 + 上下留白 + 上下边框，再留 2 像素余量
        status_log.setFixedHeight(3 * line_height + 2 * margin + 2 * frame)
        self.status_log = status_log
        return status_log

    # ── 实时监控卡片与定时刷新 ──────────────────────────────

    #建立数值监视页面
    def build_monitor_page(self, snapshot):
        #各个设备组从上到下使用垂直布局
        # 创建「实时监控」页。各个设备组会从上往下放进这个布局。
        page = QWidget()
        page_layout = QVBoxLayout(page)

        # 字典里的标签属于当前页面。以后初始化重建页面时，要重新登记新标签。
        self.values_labels.clear()

        # 传入 page_layout，表示这次需要建卡，并用启动快照填上初始读数。
        self.update_monitor_cards(snapshot, page_layout)

        # 底部留出伸缩空间，让设备组靠页面顶部排列。
        page_layout.addStretch()
        return page

    def refresh_readings(self):
        # QTimer 每次触发时，从 DataHub 拿一次最新快照。
        snapshot = BLL_GetDevicesSnapshot()
        # 省略第二个参数，page_layout 会使用默认值 None。
        # 这次复用已有卡片，只更新其中的读数。
        self.temperature_alarm_states, alarm_messages = BLL_CheckTemperatureSnapshot(
            snapshot, self.temperature_alarm_states
        )
        self.update_monitor_cards(snapshot)
        for message in alarm_messages:
            self.status_log.appendPlainText(message)
        # 这里已经在 Qt 主线程，可以直接写状态框。
        for message in take_device_errors():
            self.status_log.appendPlainText(message)

            # 现有消息格式是「日期 时间  设备名 采集异常：……」。
            # 例如「2026-11-25 15:48:49  UT3200 采集异常：……」。
            # 按空白拆开后，下标 2 是设备名。
            device = message.split(maxsplit=3)[2]
            button = {
                "UT3200": self.ut3200_button,
                "UDP": self.udp_button,
            }.get(device)

            # 只有正在采集的按钮才点。已复原的按钮再点就会重新启动。
            if button is not None and button.isChecked():
                button.click()


    def update_monitor_cards(self, snapshot,page_layout=None):
        # 建卡和刷新共用这一组循环，遍历顺序沿用 JSON 中的顺序。
        for device in snapshot["devices"]:
            # 每到一个新设备，就重新准备它自己的网格和卡片编号。
            grid = None
            card_index = 0
            for signal in device["signals"]:
                if signal["enabled"] is False:
                    continue
                signal_name = signal["name"]
                if page_layout is not None:
                    # 遇到该设备第一条可显示的信号时，才创建设备组。
                    # 同一设备接下来的卡使用grid网格布局
                    if grid is None:
                        #按照device的数量分成若干个设备组
                        group = QGroupBox(device["name"])
                        grid = QGridLayout(group)
                        #每个设备组之间垂直布局
                        page_layout.addWidget(group)

                    # 信号名称作为卡片标题。卡片里的QLabel专门显示读数。
                    card = QGroupBox(signal_name)
                    card_layout = QVBoxLayout(card)
                    label = QLabel()
                    card_layout.addWidget(label)
                    # 按信号名称登记读数标签，刷新时直接查找对应控件。
                    self.values_labels[signal_name] = label
                    #每行四张卡片，编号只统计实际显示的卡片，停用项不占位置。
                    row,column = divmod(card_index,8)
                    grid.addWidget(card,row,column)
                    card_index += 1
                else:
                    # 刷新时按名称取出建卡阶段保存的 QLabel。
                    # 当前页面里没有对应卡片时，跳过这条信号。
                    label = self.values_labels.get(signal_name)
                    if label is None:
                        continue
                # label要么是刚创建的，要么是已经存在的。
                # 两种情况都用当前信号的 value、fmt 和 unit 来设置文字。
                value = signal["value"]
                fmt = signal["fmt"]
                unit = signal["unit"]
                if value is None or value =="":
                    text = "-"
                elif fmt:
                    try:
                        # UT3200 提供数字，Dog UDP 可能提供数字字符串。
                        # 转成 float 后，"{:.1f}" 可以把 "25.64" 显示为 "25.6"。
                        text = fmt.format(float(value))
                    except (TypeError, ValueError, IndexError, KeyError):
                        text = str(value)
                else:
                    text = str(value)
                label.setText(f'{text}{unit}')
                # 越限时用红色。空样式会恢复标签的默认字体颜色。
                is_alarm = signal["id"] in self.temperature_alarm_states
                style = "color: #ff6b6b;" if is_alarm else ""

                # 颜色变化时才设置，避免每次定时刷新都重复设置。
                if label.styleSheet() != style:
                    label.setStyleSheet(style)

    # ── 按钮启停与后台结果 ─────────────────────────────────
    # 绑定点击 → 选择启停函数 → 后台执行 → 发出结果 → 主线程更新按钮。

    def bind_button_click(
        self, button, name, start, stop,
        text_start="开始采集", text_stop="结束采集",
    ):
        """设置按钮的初始状态，并在点击时传入对应的启停函数。"""
        button.setCheckable(True)
        button.setText(f"{name}{text_start}")

        # Qt 提供 checked，lambda 补齐 button_clicked 所需的其余参数。
        button.clicked.connect(
            lambda checked: self.button_clicked(
                button,
                name,
                checked,
                start,
                stop,
                start_text=text_start,
                stop_text=text_stop,
            )
        )

    def button_clicked(
        self, button, name, checked, start, stop, *,
        start_text="开始采集", stop_text="结束采集",
    ):
        """在 UI 主线程处理点击，把本次启停交给后台。"""
        operation = start if checked else stop
        next_text = stop_text if checked else start_text

        # 等待操作结果期间暂停其他启停点击。
        for capture_button in (self.udp_button, self.ut3200_button, self.csv_button):
            capture_button.setEnabled(False)

        # 这个线程只执行本次启停，持续采集和存储由 BLL 与 HAL 管理。
        button_thread = Thread(
            target=self.button_operation,
            args=(operation, button, name, checked, next_text),
            daemon=True,
        )
        try:
            button_thread.start()
            self.button_thread = button_thread
        except RuntimeError as error:
            # 线程启动失败时，当前仍在 UI 主线程，可以直接恢复按钮。
            self.update_button_after_operation(button, name, checked, next_text, error)

    def button_operation(self, operation, button, name, checked, next_text):
        """在后台调用 BLL 启停函数，并把结果发给主线程。"""
        error = None
        try:
            operation()
        except Exception as caught:
            error = caught
        self.button_result_signal.emit(button, name, checked, next_text, error)

    # 接收 button_result_signal 发来的启停结果，在 UI 主线程更新按钮。
    # 类型按顺序对应：按钮对象、设备名、点击后的勾选状态、下一次操作的文字、异常对象。
    @Slot(object, str, bool, str, object)
    def update_button_after_operation(self, button, name, checked, next_text, error):
        """在主线程恢复按钮。成功时更新文字，失败时还原勾选状态。"""
        for capture_button in (self.udp_button, self.ut3200_button, self.csv_button):
            capture_button.setEnabled(True)

        if button in (self.udp_button, self.ut3200_button, self.csv_button):
            action = "启动" if checked else "关闭"
            if error is None:
                result = "成功"
            else:
                # 第一部分是「异常类型: 主要原因」。
                # 如果 HAL 调过 error.add_note()，后面还会带上附加说明。
                parts = format_exception_only(error)
                # 把每部分里的换行和多余空白合成空格，再用分号分隔。
                # 这样一次失败在状态框里仍然是一条日志。
                detail = "；".join(" ".join(part.split()) for part in parts if part.strip())
                result = f"失败（{detail}）"

            timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
            # appendPlainText() 在末尾追加一条消息，之前的消息仍可向上滚动查看。
            self.status_log.appendPlainText(f"{timestamp}  {name} {action}{result}")

        if error is not None:
            button.setChecked(not checked)
            QMessageBox.warning(self, f"{name} 操作失败", str(error))
            return

        button.setText(f"{name}{next_text}")

    @Slot()
    def stop_on_quit(self):
        if self.button_thread is not None:
            self.button_thread.join()
        try:
            BLL_ShutDowmAll()
        except RuntimeError as error:
            # 应用已经在退出，把失败原因写到运行终端。
            print(f"退出清理失败：{error}")


# ── 程序入口 ────────────────────────────────────────────

def main():
    """加载同目录的 JSON 配置，用初始快照创建窗口，再进入 Qt 事件循环。"""
    app = QApplication([])
    config_path = Path(__file__).with_name("signals_config.json")
    BLL_JSON_InitDevices(str(config_path))
    snapshot = BLL_GetDevicesSnapshot()

    window = UI_MainWindow(snapshot)
    app.aboutToQuit.connect(window.stop_on_quit)
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
