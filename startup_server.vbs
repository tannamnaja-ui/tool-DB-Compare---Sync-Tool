Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "d:\work\งาน IM\Project\fulfill3to4"
WshShell.Run "python app.py", 0, False
