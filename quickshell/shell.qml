import Quickshell
import Quickshell.Io

ShellRoot {
    Dash {
        id: dash
        onFinished: Qt.quit()
    }

    IpcHandler {
        target: "dash"
        function toggle(): void {
            dash.dismiss()
        }
    }
}
