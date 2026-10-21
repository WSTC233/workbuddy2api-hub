' WorkBuddy Proxy 托盘启动器
' 后台运行，不显示控制台窗口
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strPath = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = strPath
' 0 = 隐藏窗口, False = 不等待返回
WshShell.Run "pythonw wb_tray.py", 0, False
WScript.Quit
