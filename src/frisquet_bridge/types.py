'Callback shapes shared by the scheduler, emulation and MQTT layers.'

from collections.abc import Awaitable, Callable

# A no-argument coroutine the service awaits to republish state after an update.
UpdateNotify = Callable[[], Awaitable[None]]

# A writer that pushes a temperature and reports whether the boiler accepted it.
TemperatureWriter = Callable[[float], Awaitable[bool]]
