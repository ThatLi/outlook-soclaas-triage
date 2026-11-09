from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
a = Analysis([str(root / 'packaging/entry.py'), str(root / 'packaging/background.py')],
    pathex=[str(root / 'src')], datas=collect_data_files('tzdata'),
    hiddenimports=['pythoncom', 'pywintypes', 'win32com.client', 'win32timezone'],
    excludes=['pytest', 'pytest_cov'], noarchive=False)
pyz = PYZ(a.pure)
cli = EXE(pyz, [s for s in a.scripts if s[0] != 'background'], [], exclude_binaries=True, name='outlook-triage', console=True, upx=False)
background = EXE(pyz, [s for s in a.scripts if s[0] != 'entry'], [], exclude_binaries=True,
    name='outlook-triage-background', console=False, upx=False)
coll = COLLECT(cli, background, a.binaries, a.datas, strip=False, upx=False, name='OutlookTriage')
