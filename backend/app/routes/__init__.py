"""The HTTP routers, one module per resource area.

``create_app`` builds the services, hands them to ``app.state`` and includes these; a route
module reaches them through the dependency functions in :mod:`app.routes.common`.
"""
