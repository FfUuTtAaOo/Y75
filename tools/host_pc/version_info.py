# -*- coding: utf-8 -*-
#
# PyInstaller version resource for Y75_Host.exe  (exe 属性页里可见版本号)
#
# !!  IMPORTANT  !!
# PyInstaller does NOT import this file - it reads the text and runs
#     eval(text)
# inside PyInstaller/utils/win32/versioninfo.py. Therefore this file must
# contain ONE SINGLE EXPRESSION and nothing else:
#   * no assignment, no import, no "if", no print
#   * the bare names (VSVersionInfo, FixedFileInfo, ...) already exist in
#     the namespace that eval() uses, so you just call them.
# Adding a statement like "VSVersionInfo = None" makes the build fail with:
#   ValueError: Failed to deserialize VSVersionInfo from text-based representation!

VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 0, 0, 0),
    prodvers=(1, 0, 0, 0),
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404B0',
          [
            StringStruct('CompanyName', 'Y75'),
            StringStruct('FileDescription', 'Y75 六维力传感器上位机 (RS485)'),
            StringStruct('FileVersion', '1.0.0.0'),
            StringStruct('InternalName', 'Y75_Host'),
            StringStruct('LegalCopyright', ''),
            StringStruct('OriginalFilename', 'Y75_Host.exe'),
            StringStruct('ProductName', 'Y75 Host'),
            StringStruct('ProductVersion', '1.0.0.0')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
