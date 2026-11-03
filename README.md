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
