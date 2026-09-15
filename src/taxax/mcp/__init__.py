def create_server(*args, **kwargs):
    from .server import create_server as factory

    return factory(*args, **kwargs)


__all__ = ["create_server"]
