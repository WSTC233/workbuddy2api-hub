' WorkBuddy Proxy 启动脚本 - 看门狗模式
' 只启动看门狗，由它启动并守护所有服务（代理/托盘/更新页）
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strPath = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = strPath

' 使用 Anaconda 的 Python（TRAE 自带 Python 更新后已不存在）
pythonwPath = "C:\Users\ROG\anaconda3\pythonw.exe"

WshShell.Run """" & pythonwPath & """ wb_watchdog.py", 0, False

WScript.Quit
