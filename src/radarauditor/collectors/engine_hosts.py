from radarauditor.collectors.patterns import host_of


def is_engine_host(host: str, engine: str) -> bool:
    return engine in host.split(".")[:-1]


def only_engine_hosts(organic: list[tuple[int, str]], engine: str) -> bool:
    return bool(organic) and all(
        (host := host_of(url)) is not None and is_engine_host(host, engine) for _, url in organic
    )
