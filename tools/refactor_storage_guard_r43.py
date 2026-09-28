from pathlib import Path

p=Path('veritas_intelligence.py')
src=p.read_text(encoding='utf-8')
marker='# VERITAS V90 STORAGE CIRCUIT BREAKER R42'
main_guard="if __name__ == '__main__':"
start=src.rfind(marker)
main=src.rfind(main_guard)

if start < 0:
    raise SystemExit('R42 marker not found')
if main < 0 or main <= start:
    raise SystemExit('main guard not found after R42 marker')

replacement=(
    "from veritas_storage_guard import install_storage_guard as _v90_install_storage_guard\n"
    "_v90_install_storage_guard(globals())\n\n"
)

p.write_text(src[:start]+replacement+src[main:],encoding='utf-8')

for q in (
    Path('.github/workflows/veritas-storage-refactor.yml'),
    Path('tools/refactor_storage_guard_r43.py'),
):
    if q.exists():
        q.unlink()

print('VERITAS_STORAGE_GUARD_R43_REFACTORED')
