# xr-session-manager

实际控制器/头显会话的启动停止、数据转发与清理。


构建入口和依赖见下方；工程 Wiki/Blog 与跨仓库规划集中在 [openkylin-xr-docs](https://gitee.com/bigbits/openkylin-xr-docs)。

## 打包

`python3 tools/package.py --output /new/package`

src/ 导自已发布的 controller-production-runtime 会话包，integration/ 包含后续 present-offset 入口。需要另行提供 kernel、设备/跟踪/VR 组件和 vendor 运行库。打包命令不启动服务、不写设备；安装与候选选择仍按原有设备窗口和回退流程执行。

## openKylin 打包

本仓库包含独立的 `debian/` 配方。使用目标发行线的干净构建环境运行 `dpkg-buildpackage -us -uc -b`，预先准备 Build-Depends 和离线构建输入。包构建、目标系统安装和设备验收分别记录。

当前迁移不自动启用竞争采集、切换活动运行时或写设备启动分区；已有验证基线继续保留。

现有会话入口保留已用系统的受控检查与显式参数。新发行版的路径、桌面单元和运行资源需在型号 profile 中配置并验证；包安装不将其视为已完成的一体机运行环境。


## 原生场景与可选手输入

会话管理器的程序安装到 `/usr/lib/xr/session-manager`。`xr-shell-session`
是独立的前台场景入口，继续由镜像/场景工具包维护在 `/usr/lib/xr/session`，
两个包不覆盖同一个文件。使用 `QIYU_VR_SESSION_PROFILE` 显式选择发行版与场景；
`configure_boot.py` 只更新启动选择，包安装不启动相机或 XR。

可选 `hand_tracking` 配置由同一个追踪监督器启动消费者，帧仍来自唯一的
头部采集所有者。选定的头部插件须给出绝对路径与 SHA-256，现有手柄配置
保持独立。监督器记录手服务状态，手服务异常释放输入，头部生产者继续运行。
正常退出先记录 shutdown 意图，按 systemd 依赖顺序停止手消费者和头采集；
终态检查服务 Result 与退出码，避免把正常的依赖停机记为手服务故障。
