"""最小四通道 Measurement Module 示例，不需要继承框架基类。"""

from labcontrol.module_api import ModuleAPI, ModuleError


class Module:
    columns = {
        "R1": "Ohm",
        "R2": "Ohm",
        "R3": "Ohm",
        "R4": "Ohm",
        "StatusCode": "",
    }
    display_columns = ("R1", "R2", "R3", "R4")
    slots = 4

    def __init__(self) -> None:
        self.ready = False

    def open(self, api: ModuleAPI):
        self.ready = True
        api.status({"State": "Ready"})
        return {"State": "Ready"}

    def measure(self, slot: int, api: ModuleAPI):
        if not self.ready:
            raise ModuleError("Module is not open", "NOT_READY")
        if slot not in {1, 2, 3, 4}:
            raise ModuleError("Invalid logical slot", "INVALID_SLOT", str(slot))
        api.sleep(0)
        temperature = float(
            api.instruments().get("temperature", {}).get("current") or 300.0
        )
        return {f"R{slot}": 100.0 + slot + temperature / 1000.0, "StatusCode": 0}

    def close(self, api: ModuleAPI):
        self.ready = False
        api.status({"State": "Disabled"})
        return {"State": "Disabled"}
