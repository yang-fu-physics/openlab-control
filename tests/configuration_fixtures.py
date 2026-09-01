from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from labcontrol.config import AppConfig, load_config  # noqa: E402


SIMULATED_INSTRUMENTS = ROOT / "tests" / "fixtures" / "simulated_instruments"


def write_simulated_configuration(project_root: Path) -> Path:
    """Write the standard three-instrument API v4 test configuration."""

    configs = project_root / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    general = configs / "general.toml"
    shutil.copy2(ROOT / "configs" / "general.toml", general)
    # 既有端到端用例继续验证兼容宽表；紧凑格式用例会显式开启发行默认值。
    general.write_text(
        general.read_text(encoding="utf-8").replace(
            "compact_measurement_data = true",
            "compact_measurement_data = false",
        ),
        encoding="utf-8",
    )
    destination = configs / "instruments"
    destination.mkdir()
    for source in sorted(SIMULATED_INSTRUMENTS.glob("*.toml")):
        shutil.copy2(source, destination / source.name)
    return general


def load_simulated_config() -> AppConfig:
    """Load the standard test instruments while keeping project paths at ROOT."""

    with tempfile.TemporaryDirectory() as temporary:
        config = load_config(write_simulated_configuration(Path(temporary)))
    return replace(config, source_path=ROOT / "configs" / "general.toml")
