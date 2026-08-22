DOMAIN = "wiener_netze_smart_meter"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_API_KEY = "api_key"

# Optional: entity_id of a price sensor giving the all-in price (e.g. the EPEX
# Spot "total price" sensor). When set, hourly cost statistics are computed.
CONF_PRICE_ENTITY = "price_entity_id"

# Currency of the price entity / cost statistics.
COST_CURRENCY = "EUR"

UPDATE_INTERVAL_HOURS = 12

# Days of quarter-hour history to pull on first run (no statistics yet).
# Raise for more historical backfill on the Energy dashboard.
BACKFILL_DAYS = 30
