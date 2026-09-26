' Launch the OP3T Webcam GUI with NO console window.
' Double-click this file. (The GUI sets up the USB tunnel itself.)
'
' We resolve the real pythonw.exe from the registry (PEP 514) instead of relying on
' "pythonw" on PATH -- on Windows that often resolves to the Microsoft Store app-execution
' alias, which Windows Script Host cannot launch (error 800A0046 "Permission denied").

Dim fso, sh, scriptDir, target, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = scriptDir & "\op3t_webcam.py"

pyw = FindPythonW()
If pyw = "" Then pyw = "pythonw"   ' last resort: hope PATH has a real one

sh.Run """" & pyw & """ """ & target & """", 0, False

' Find the newest installed pythonw.exe via HKCU then HKLM Software\Python\PythonCore\<ver>\InstallPath
Function FindPythonW()
    Dim reg, hives, h, vers, v, val
    FindPythonW = ""
    On Error Resume Next
    Set reg = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\default:StdRegProv")
    If Err.Number <> 0 Then Exit Function
    hives = Array(&H80000001, &H80000002)   ' HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE
    Dim hi
    For hi = 0 To UBound(hives)
        h = hives(hi)
        vers = Empty
        reg.EnumKey h, "Software\Python\PythonCore", vers
        If IsArray(vers) Then
            For Each v In vers
                val = ""
                reg.GetStringValue h, "Software\Python\PythonCore\" & v & "\InstallPath", "WindowedExecutablePath", val
                If (Not IsNull(val)) And val <> "" Then
                    If fso.FileExists(val) Then
                        FindPythonW = val
                        Exit Function
                    End If
                End If
            Next
        End If
    Next
End Function
