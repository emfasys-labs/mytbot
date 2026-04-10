Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\mytbot"
WshShell.Run "python run.py", 0, False
