# LagZero 打包说明

## 源码运行

```bat
LagZero.bat
```

脚本会使用当前 PATH 中的 Python 启动 `main.py`；程序会自行请求管理员权限。

## 便携版

```bat
build_windows.bat
```

产物为 `dist\LagZero.exe`。PresentMon 已内嵌，目标机器不需要 Python 或 PresentMon；运行数据保存在 `%LOCALAPPDATA%\LagZero\data`。

## 安装包

1. 安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)。
2. 运行 `build_windows.bat`。

脚本检测到 `ISCC.exe` 后会自动编译 `installer\LagZero.iss`，产物为 `dist\LagZero-1.0.0-setup.exe`。未安装 Inno Setup 时仍会生成便携版。
