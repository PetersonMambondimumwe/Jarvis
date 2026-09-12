' VBScript to launch JARVIS Wake Word Daemon silently in the background
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
rootDir = fso.GetParentFolderName(scriptDir)

cmd = "python """ & rootDir & "\core\wake_word.py"""
WshShell.CurrentDirectory = rootDir
WshShell.Run cmd, 0, False
