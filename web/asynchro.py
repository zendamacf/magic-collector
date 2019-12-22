# Standard library imports
import os

# Local imports
from web import (
	app, scryfall, tcgplayer, openexchangerates, collection
)
from flasktools import get_static_file, fetch_image
from flasktools.celery import setup_celery
from flasktools.db import mutate_query

celery = setup_celery(app)


def set_icon_filename(code: str) -> str:
	return get_static_file('/images/set_icon_{}.svg'.format(code))


@celery.task(queue='collector')
def get_set_icon(code: str) -> None:
	filename = set_icon_filename(code)
	if not os.path.exists(filename):
		url = scryfall.get_set(code)['icon_svg_uri']
		fetch_image(filename, url)


def card_art_filename(cardid: int) -> str:
	return get_static_file('/images/card_art_{}.jpg'.format(cardid))


@celery.task(queue='collector')
def get_card_art(cardid: int, code: str, collectornumber: str) -> None:
	filename = card_art_filename(cardid)
	if not os.path.exists(filename):
		url = scryfall.get(code, collectornumber)['arturl']
		fetch_image(filename, url)


def card_image_filename(cardid: int) -> str:
	return get_static_file('/images/card_image_{}.jpg'.format(cardid))


@celery.task(queue='collector')
def get_card_image(cardid: int, code: str, collectornumber: str) -> None:
	filename = card_image_filename(cardid)
	if not os.path.exists(filename):
		url = scryfall.get(code, collectornumber)['imageurl']
		fetch_image(filename, url)


@celery.task(queue='collector')
def fetch_prices(cards: list, tcgplayer_token: str) -> None:
	for c in cards:
		if c['productid'] is None:
			print('Searching for TCGPlayer ID for {} ({}).'.format(c['name'], c['set_name']))
			c['productid'] = tcgplayer.search(c, token=tcgplayer_token)
			if c['productid'] is not None:
				mutate_query(
					"UPDATE printing SET tcgplayer_productid = %s WHERE id = %s",
					(c['productid'], c['id'],)
				)

	# Filter out cards without tcgplayerid to save requests
	cards = [c for c in cards if c['productid'] is not None]
	bulk_lots = ([cards[i:i + 250] for i in range(0, len(cards), 250)])
	prices = {}
	for lot in bulk_lots:
		card_dict = {str(c['id']): str(c['productid']) for c in lot if c['productid'] is not None}
		prices.update(
			tcgplayer.get_price(
				card_dict,
				token=tcgplayer_token
			)
		)

	updates = []
	for cardid, price in prices.items():
		# Only update if we received have prices
		if price['normal'] is not None or price['foil'] is not None:
			updates.append({
				'price': price['normal'],
				'foilprice': price['foil'],
				'pricetype': price['type'],
				'id': cardid
			})
	mutate_query(
		"SELECT set_price(%(id)s, %(price)s::MONEY, %(foilprice)s::MONEY, %(pricetype)s)",
		updates,
		executemany=True
	)
	print('Updated prices for {} cards.'.format(len(updates)))


@celery.task(queue='collector')
def fetch_rates() -> None:
	print('Fetching exchange rates')
	rates = openexchangerates.get()
	updates = [{'code': code, 'rate': rate} for code, rate in rates.items()]
	mutate_query("SELECT update_rates(%(code)s, %(rate)s)", updates, executemany=True)
	print('Updated exchange rates')


@celery.task(queue='collector')
def refresh_from_scryfall(query: str) -> None:
	resp = scryfall.search(query)
	collection.import_cards(resp)
