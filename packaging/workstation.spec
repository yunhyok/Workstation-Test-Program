from pathlib import Path

project = Path(SPECPATH).parent
scripts = project / 'tools' / 'engine_studies' / 'workstation'
datas = [(str(p), 'tools/engine_studies/workstation') for p in scripts.glob('*.py')]
datas += [(str(scripts / 'README.md'), 'tools/engine_studies/workstation')]
datas += [(str(project / 'docs' / 'engine' / 'W15_PLAN_20260921.md'), 'docs/engine')]
datas += [(str(project / 'README.md'), '.'), (str(project / 'config.example.json'), '.')]
datas += [(str(project / 'THIRD_PARTY_NOTICES.md'), '.')]
datas += [(str(p), 'licenses') for p in (project / 'packaging' / 'licenses').glob('*.txt')]
a = Analysis([str(project / 'app.py')], pathex=[str(project), str(scripts)],
             binaries=[], datas=datas, hiddenimports=['ws_validate', 'ws_agent', 'ws_ctl', 'study_report', 'common'],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['numpy', 'scipy', 'matplotlib', 'cupy', 'nvmath', 'PySide6', 'spd_pi_engine', 'spd_decap_pi'],
             noarchive=False)
pyz = PYZ(a.pure)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WorkstationTest',
          console=True, debug=False, strip=False, upx=False)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='WorkstationTestProgram',
          console=False, debug=False, strip=False, upx=False)
coll = COLLECT(cli, gui, a.binaries, a.datas, strip=False, upx=False, name='WorkstationTestProgram')
