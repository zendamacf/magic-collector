
CREATE TABLE IF NOT EXISTS enduser (
	id SERIAL PRIMARY KEY,
	firstname TEXT NOT NULL,
	surname TEXT NOT NULL,
	email TEXT NOT NULL,
	username TEXT NOT NULL,
	password TEXT NOT NULL,
	ipaddr INET,
	currencycode TEXT
)WITH OIDS;

CREATE TABLE IF NOT EXISTS card_set (
	id SERIAL PRIMARY KEY,
	name TEXT NOT NULL,
	code TEXT NOT NULL,
	released DATE NOT NULL,
	tcgplayer_groupid INTEGER
)WITH OIDS;

CREATE TABLE IF NOT EXISTS card_type (
	id SERIAL PRIMARY KEY,
	name TEXT NOT NULL
)WITH OIDS;

CREATE TABLE IF NOT EXISTS card (
	id SERIAL PRIMARY KEY,
	collectornumber TEXT NOT NULL,
	multiverseid INTEGER,
	name TEXT NOT NULL,
	card_setid INTEGER NOT NULL REFERENCES card_set(id) ON DELETE CASCADE,
	colors TEXT,
	rarity CHARACTER,
	multifaced BOOLEAN NOT NULL DEFAULT FALSE,
	price MONEY,
	foilprice MONEY,
	tcgplayer_productid TEXT,
	cmc NUMERIC,
	typeline TEXT,
	manacost TEXT,
	card_typeid INTEGER REFERENCES card_type(id) ON DELETE SET NULL
)WITH OIDS;

CREATE TABLE IF NOT EXISTS user_card (
	id SERIAL PRIMARY KEY,
	cardid INTEGER NOT NULL REFERENCES card(id) ON DELETE CASCADE,
	userid INTEGER NOT NULL REFERENCES app.enduser(id) ON DELETE CASCADE,
	quantity INTEGER NOT NULL,
	foil BOOLEAN NOT NULL DEFAULT false
)WITH OIDS;

CREATE TABLE IF NOT EXISTS currency (
	id SERIAL PRIMARY KEY,
	code TEXT NOT NULL,
	exchangerate NUMERIC NOT NULL
)WITH OIDS;

CREATE TABLE IF NOT EXISTS price_history (
	id SERIAL PRIMARY KEY,
	cardid INTEGER NOT NULL REFERENCES card(id) ON DELETE CASCADE,
	price MONEY,
	foilprice MONEY,
	created DATE NOT NULL DEFAULT current_date
)WITH OIDS;

CREATE TABLE IF NOT EXISTS deck (
	id SERIAL PRIMARY KEY,
	name TEXT,
	userid INTEGER NOT NULL REFERENCES app.enduser(id) ON DELETE CASCADE,
	deleted BOOLEAN NOT NULL DEFAULT false,
	cardartid INTEGER REFERENCES card(id) ON DELETE SET NULL,
	formatid INTEGER NOT NULL REFERENCES format(id)
)WITH OIDS;

CREATE TABLE IF NOT EXISTS deck_card (
	id SERIAL PRIMARY KEY,
	deckid INTEGER NOT NULL REFERENCES deck(id) ON DELETE CASCADE,
	cardid INTEGER NOT NULL REFERENCES card(id) oN DELETE CASCADE,
	quantity INTEGER NOT NULL,
	section TEXT NOT NULL
)WITH OIDS;

CREATE TABLE IF NOT EXISTS format (
	id SERIAL PRIMARY KEY,
	name TEXT NOT NULL
)WITH OIDS;

CREATE TABLE IF NOT EXISTS import (
	id SERIAL PRIMARY KEY,
	filename TEXT NOT NULL,
	userid INTEGER NOT NULL REFERENCES app.enduser(id) ON DELETE CASCADE,
	uploaded TIMESTAMP NOT NULL DEFAULT now()
)WITH OIDS;

CREATE TABLE IF NOT EXISTS import_row (
	id SERIAL PRIMARY KEY,
	importid INTEGER NOT NULL REFERENCES import(id) ON DELETE CASCADE,
	cardid INTEGER NOT NULL REFERENCES card(id) ON DELETE CASCADE,
	quantity INTEGER NOT NULL,
	foil BOOLEAN NOT NULL DEFAULT false,
	complete BOOLEAN NOT NULL DEFAULT false
)WITH OIDS;
