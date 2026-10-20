# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller, onedir.

Not onefile: it unpacks sixty megabytes to a temporary directory on every
service start, which is an antivirus trigger, a delay on every restart, and a
moving sys._MEIPASS that confuses a service manager pointed at it.
"""
import pathlib

root = pathlib.Path(SPECPATH).parent
src = root / 'src'

a = Analysis(
    [str(root / 'packaging' / 'entry.py')],
    pathex=[str(src)],
    datas=[
        (str(src / 'zkbridge' / 'web' / 'templates'), 'zkbridge/web/templates'),
        (str(src / 'zkbridge' / 'web' / 'static'), 'zkbridge/web/static'),
        (str(src / 'zkbridge' / 'vendor' / 'ZK_SHA256SUMS'), 'zkbridge/vendor'),
    ],
    hiddenimports=[
        'zkbridge', 'zkbridge.__main__',
        'waitress', 'waitress.server',
        'zkbridge.vendor.zk', 'zkbridge.vendor.zk.base',
        'zkbridge.vendor.zk.const', 'zkbridge.vendor.zk.user',
        'zkbridge.vendor.zk.finger', 'zkbridge.vendor.zk.attendance',
        'zkbridge.vendor.zk.exception',
    ],
    excludes=['tkinter', 'unittest', 'pydoc', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='zkbridge', console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='zkbridge', upx=False)
