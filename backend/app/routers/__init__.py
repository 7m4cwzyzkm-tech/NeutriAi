from . import billing, fitness, lifestyle, profiles, recipes, scans, social  # noqa: F401

ALL_ROUTERS = [
    profiles.router,
    scans.router,
    lifestyle.router,
    fitness.router,
    recipes.router,
    social.router,
    billing.router,
]
