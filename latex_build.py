from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ENGINES = ("pdflatex", "xelatex", "lualatex", "tectonic")


def find_engine() -> str | None:
    for engine in ENGINES:
        if shutil.which(engine):
            return engine
    return None


def build_tex(tex_path: Path, engine: str) -> int:
    command = [engine]
    if engine == "tectonic":
        command.extend(["--keep-logs", "--keep-intermediates", str(tex_path)])
    else:
        command.extend(
            [
                "-interaction=nonstopmode",
                "-halt-on-error",
                str(tex_path.name),
            ]
        )

    result = subprocess.run(command, cwd=tex_path.parent, check=False)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile a .tex file with a locally installed LaTeX engine."
    )
    parser.add_argument("tex_file", help="Path to the .tex file")
    args = parser.parse_args()

    tex_path = Path(args.tex_file).expanduser().resolve()
    if not tex_path.exists():
        print(f"파일을 찾을 수 없습니다: {tex_path}")
        return 1
    if tex_path.suffix.lower() != ".tex":
        print(f".tex 파일만 처리할 수 있습니다: {tex_path.name}")
        return 1

    engine = find_engine()
    if not engine:
        print("로컬 LaTeX 엔진을 찾지 못했습니다.")
        print("현재 PC에는 pdflatex/xelatex/lualatex/tectonic 이 설치되어 있지 않습니다.")
        print("오버리프 라이선스가 있어도 공식 로컬 컴파일 API처럼 바로 붙여 쓰는 방식은 아닙니다.")
        print("로컬 자동 변환을 원하시면 TeX Live, MiKTeX, 또는 Tectonic 설치가 먼저 필요합니다.")
        return 2

    print(f"{engine} 로 컴파일합니다: {tex_path.name}")
    code = build_tex(tex_path, engine)
    if code == 0:
        print(f"완료: {tex_path.with_suffix('.pdf')}")
    else:
        print("컴파일에 실패했습니다. 생성된 로그 파일을 확인해 주세요.")
    return code


if __name__ == "__main__":
    sys.exit(main())
