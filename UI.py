
from pathlib import Path
from PySide6.QtCore import QTimer,Qt,Signal,Slot
from PySide6.QtWidgets import (QApplication, QGridLayout, QGroupBox,QHBoxLayout,
                               QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget,
                               QPushButton,QMessageBox,QScrollArea)

from BLL_datahub_v import (BLL_GetDevicesSnapshot, BLL_JSON_InitDevices, BLL_StartDog,
                           BLL_StartUT3200,BLL_StopUT3200,BLL_StopDog)

class UI_MainWindow(QMainWindow):
    # 参数依次是按钮、设备名、点击后的勾选状态、成功后的按钮文字、异常对象。成功时，最后一个参数传 None。
    capture_finished = Signal(object,str,bool,str,object)
    def __init__(self,snapshot):
        super().__init__()
        self.setWindowTitle("机器狗监控台")
        self.resize(900, 600)
        self.values_labels = {}  # 按信号名称查找读数标签，配置应保证名称全局唯一。
        self.tabs = QTabWidget()
        monitor_page = self.build_monitor_page(snapshot)

        #QScrollArea是 Qt 的滚动容器
        card_scroll = QScrollArea()
        # True 允许 Qt 调整内部页面的尺寸，让卡片页尽量贴合滚动区。
        # setWidget() 指定滚动区里面真正显示的控件。
        card_scroll.setWidgetResizable(True)
        card_scroll.setWidget(monitor_page)
        # 280 是卡片区的高度上限，单位是 Qt 逻辑像素。
        # 卡片少时区域可以更矮，卡片多时从这里往下滚。
        card_scroll.setMaximumHeight(280)
        # 横向：卡片页比可见区域宽时，显示底部的滚动条。
        card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 纵向：卡片页比可见区域宽时，显示底部的滚动条。
        card_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 卡片页布局所需的最小宽度，已包含卡片和布局间距。
        card_min_width = monitor_page.minimumSizeHint().width()
        # 即使当前没有纵向滚动条，也先预留它出现时所需的边框宽度。
        scrollbar_width = card_scroll.verticalScrollBar().sizeHint().width()
        frame_width = 2 * card_scroll.frameWidth()
        # 这个限制会传递到主窗口，窗口拖窄时会在足够显示八列的位置停住。
        card_scroll.setMinimumWidth(card_min_width + scrollbar_width + frame_width)
        # self.tabs.addTab(monitor_page, "实时监控")
        # self.tabs.addTab(QLabel("待完成"), "传感器详情")
        # self.tabs.addTab(QLabel("待完成"), "热成像视频")
        # 主窗口中央放一个容器，里面从上到下放按钮栏和标签页。
        central = QWidget()
        main_layout = QVBoxLayout(central)

        # 水平布局按添加顺序从左往右排列控件。
        button_row = QHBoxLayout()
        button_row.setSpacing(8)  # 两个按钮之间留 8 像素。

        # 把伸缩空间放在按钮前面，窗口变宽时，空白会留在左侧。
        button_row.addStretch()
        self.udp_button = QPushButton()
        self.bind_capture_button(self.udp_button,"UDP", BLL_StartDog,BLL_StopDog )
        self.ut3200_button = QPushButton("UT3200开始采集")
        self.bind_capture_button(self.ut3200_button,"UT3200", BLL_StartUT3200,BLL_StopUT3200 )
        self.IRCam_button = QPushButton("IRCam开始采集")
        self.csv_button = QPushButton("CSV开始存储")
        # self.bind_capture_button(self.csv_button,"CSV", )
        button_row.addWidget(self.udp_button)
        button_row.addWidget(self.ut3200_button)
        button_row.addWidget(self.IRCam_button)
        button_row.addStretch(1)
        button_row.addWidget(self.csv_button)

        # 先放顶部按钮栏，再让标签页占据剩余高度。
        main_layout.addLayout(button_row)
        main_layout.addWidget(card_scroll)
        # 底部这一行用横向布局。12 是左右两个面板之间的间距。
        panel_row = QHBoxLayout()
        panel_row.setSpacing(12)

        # QGroupBox 是带标题的容器，括号里的文字显示在框的上边缘。
        # QVBoxLayout(sensor_panel) 把竖向布局安装在左边这个框上。
        sensor = QGroupBox("传感器详情")
        sensor_layout = QVBoxLayout(sensor)
        sensor_label = QLabel("待完成")
        #标签位置为水平和垂直都居中
        sensor_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sensor_layout.addWidget(sensor_label)

        ircam = QGroupBox("热成像视频")
        ircam_layout = QVBoxLayout(ircam)
        self.ircam_label = QLabel("待完成")
        self.ircam_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ircam_layout.addWidget(self.ircam_label)

        # 这两个 1 是横向伸缩权重，左右按 1:1 分配空间。
        panel_row.addWidget(sensor, 1)
        panel_row.addWidget(ircam, 1)
        # 360 是每个框的高度上限，单位是 Qt 逻辑像素。
        # 窗口较矮时，两个框仍然可以缩小。
        # sensor.setMaximumHeight(360)
        # ircam.setMaximumHeight(360)
        # 1 让这一行继续分配竖向空间，直到两个框达到高度上限。
        main_layout.addLayout(panel_row, 1)
        # 在两个框下面放一块可伸缩的空白，接住全屏时多出来的空间。
        # main_layout.addStretch()

        self.setCentralWidget(central)
        # 窗口建好后，在 Qt 主线程定期读取快照并刷新读数。
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_readings)
        self.refresh_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.refresh_timer.start(100)  # 单位是毫秒，目标每秒刷新10次。

    def toggle_capture(self,button,name,checked,start,stop,*,
                       start_text = "开始采集",stop_text="结束采集"
                       ):
        """按按钮状态执行启停，并在成功后更新文字。"""
        try:
            if checked:
                start()
            else:
                stop()
        except Exception as error:
            # 操作失败时恢复点击前的选中状态，并保留原文字。
            button.setChecked(not checked)
            QMessageBox.warning(self, f"{name} 操作失败", str(error))
            return
        action_text = stop_text if checked else start_text
        # 按钮显示下一次点击能执行的动作。
        button.setText(f'{name}{action_text}')

    def bind_capture_button(self,button,name,start,stop,
                            text_start="开始采集",text_stop="结束采集"
                            ):
        """设置采集按钮的初始状态并连接点击信号。"""
        button.setCheckable(True)
        button.setText(f'{name}{text_start}')

        button.clicked.connect(
            lambda checked: self.toggle_capture(
                button, name, checked, start, stop,
                start_text=text_start,stop_text=text_stop
            )
        )

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
        self.update_monitor_cards(snapshot)

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


def main():
    app = QApplication([])
    # 使用与 UI.py 同目录的信号配置文件。
    config_path = Path(__file__).with_name("signals_config.json")

    # 加载 JSON 到 DataHub，再用初始快照的名称、单位和顺序创建监控卡片。
    BLL_JSON_InitDevices(str(config_path))
    snapshot = BLL_GetDevicesSnapshot()

    # 创建并显示主窗口。关闭最后一个窗口后，Qt 默认退出事件循环。
    window = UI_MainWindow(snapshot)
    window.show()
    app.exec()

if __name__ == "__main__":
    main()
