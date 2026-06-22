"""Socket.IO event handlers.

Connection authentication and core events are defined in app.main.py
for access to the app state (DB, settings). This module is reserved for
additional domain-specific event handlers that can be imported and
registered separately.

To add new events, import the sio instance from app.main and register:
    from app.main import sio

    @sio.event
    async def my_custom_event(sid, data):
        ...
"""
