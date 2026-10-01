#!/bin/bash
# 一键构建 mc-ha-exporter.app：
#   产出 ha-integration/dist/mc-ha-exporter.app，并复制到 ~/Documents/MC/ 双击使用。
# 原理：标准 .app 包，Contents/MacOS 里是一个 bash 引导，exec /usr/bin/python3
#   运行 Resources/manager.py；manager 负责把 exporter.py 装进 ~/Library 并托管
#   launchd 代理（mc.ha-exporter）。本机自产无 quarantine，Gatekeeper 不拦。
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="mc-ha-exporter.app"
STAGING="dist/$APP_NAME"
DEST_DIR="$HOME/Documents/MC"

rm -rf "$STAGING"
mkdir -p "$STAGING/Contents/MacOS" "$STAGING/Contents/Resources"
cp app/Info.plist      "$STAGING/Contents/Info.plist"
cp exporter/exporter.py "$STAGING/Contents/Resources/exporter.py"
cp app/manager.py       "$STAGING/Contents/Resources/manager.py"

BIN="$STAGING/Contents/MacOS/mc-ha-exporter"
# 首选：swiftc 编译菜单栏常驻主程序（顶部状态栏实时显示 MC 状态）。
# Swift 规定顶层代码必须叫 main.swift，拷到临时目录改名再编译。
BUILD_DIR="$(mktemp -d)"
cp app/statusbar.swift "$BUILD_DIR/main.swift"
if swiftc "$BUILD_DIR/main.swift" -o "$BIN" 2>"$BUILD_DIR/err.log"; then
    echo "菜单栏主程序: swiftc 编译成功"
else
    echo "警告: swiftc 编译失败，回退为弹窗面板模式。错误如下："
    head -20 "$BUILD_DIR/err.log"
    cat > "$BIN" <<'LAUNCH'
#!/bin/bash
exec /usr/bin/python3 "$(dirname "$0")/../Resources/manager.py" "$@"
LAUNCH
    chmod +x "$BIN"
fi
rm -rf "$BUILD_DIR"

mkdir -p "$DEST_DIR"
rm -rf "$DEST_DIR/$APP_NAME"
cp -R "$STAGING" "$DEST_DIR/$APP_NAME"

echo "已构建: $(pwd)/$STAGING"
echo "已安装: $DEST_DIR/$APP_NAME （双击即可；CLI: Resources/manager.py --status）"
