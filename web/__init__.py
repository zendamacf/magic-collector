# Standard library imports
import os
import logging
from logging.handlers import SMTPHandler

# Third party imports
from flask import (
	request, session, jsonify, send_from_directory, flash, redirect, url_for,
	render_template
)

# Local imports
from web import collection, deck, scryfall, tcgplayer, openexchangerates
from sitetools.utility import (
	BetterExceptionFlask, is_logged_in, params_to_dict,
	login_required, check_image_exists, check_login, fetch_query,
	mutate_query, disconnect_database, handle_exception,
	setup_celery, check_celery_running
)

app = BetterExceptionFlask(__name__)

app.config.from_pyfile('site_config.cfg')
app.secret_key = app.config['SECRETKEY']

celery = setup_celery(app)

app.jinja_env.globals.update(is_logged_in=is_logged_in)

if not app.debug:
	ADMINISTRATORS = [app.config['TO_EMAIL']]
	msg = 'Internal Error on collector'
	mail_handler = SMTPHandler('127.0.0.1', app.config['FROM_EMAIL'], ADMINISTRATORS, msg)
	mail_handler.setLevel(logging.CRITICAL)
	app.logger.addHandler(mail_handler)


@app.errorhandler(500)
def internal_error(e):
	return handle_exception


@app.teardown_appcontext
def teardown(error):
	disconnect_database()


@app.route('/favicon.ico')
@app.route('/robots.txt')
@app.route('/sitemap.xml')
def static_from_root():
	return send_from_directory(app.static_folder, request.path[1:])


@app.route('/login', methods=['GET', 'POST'])
def login():
	if is_logged_in():
		return redirect(url_for('home'))

	if request.method == 'POST':
		params = params_to_dict(request.form)

		ok = check_login(params.get('username'), params.get('password'))

		if ok:
			return redirect(url_for('home'))
		else:
			flash('Login failed.', 'danger')
			return redirect(url_for('login'))

	return render_template('login.html')


@app.route('/logout', methods=['GET'])
def logout():
	session.pop('userid', None)
	return redirect(url_for('login'))


@app.route('/', methods=['GET'])
@login_required
def home():
	return render_template('collection.html', active='collection')


@app.route('/get_sets', methods=['GET'])
@login_required
def get_sets():
	sets = fetch_query("SELECT id, name, code FROM card_set ORDER BY released DESC")
	for s in sets:
		s['iconurl'] = scryfall.get_set_icon(s['code'])

	return jsonify(sets=sets)


@app.route('/get_collection', methods=['GET'])
@login_required
def get_collection():
	params = params_to_dict(request.args)
	resp = collection.get(params)

	return jsonify(**resp)


@app.route('/collection/card/add', methods=['POST'])
@login_required
def collection_card_add():
	params = params_to_dict(request.form, bool_keys=['foil'])
	resp = {}

	if params.get('cardid'):
		collection.add(params['cardid'], params['foil'], params['quantity'])
	else:
		resp['error'] = 'No card selected.'

	return jsonify(**resp)


@app.route('/collection/card/edit', methods=['POST'])
@login_required
def collection_card_edit():
	params = params_to_dict(request.form, bool_keys=['foil'])

	update_current = True
	existing = fetch_query(
		"SELECT * FROM user_card WHERE id = %s AND userid = %s",
		(params['user_cardid'], session['userid'],),
		single_row=True
	)
	if existing['foil'] != params['foil']:
		# Foil has changed, need to check for opposite record
		opposite = fetch_query(
			"SELECT * FROM user_card WHERE cardid = %s AND userid = %s AND foil != %s",
			(existing['cardid'], session['userid'], existing['foil'],),
			single_row=True
		)
		if opposite:
			# There's an opposite record, update this instead
			mutate_query(
				"UPDATE user_card SET quantity = quantity + %s WHERE id = %s",
				(params['quantity'], opposite['id'],)
			)
			mutate_query("DELETE FROM user_card WHERE id = %s", (params['user_cardid'],))
			update_current = False

	if (update_current):
		if int(params['quantity']) > 0:
			mutate_query(
				"UPDATE user_card SET quantity = %s, foil = %s WHERE id = %s AND userid = %s",
				(params['quantity'], params['foil'], params['user_cardid'], session['userid'],)
			)
		else:
			mutate_query(
				"DELETE FROM user_card WHERE id = %s AND userid = %s",
				(params['user_cardid'], session['userid'],)
			)

	return jsonify()


@app.route('/search', methods=['GET'])
@login_required
def search():
	params = params_to_dict(request.args)
	results = []

	if params.get('query'):
		search = '%' + params['query'] + '%'
		qry = """SELECT c.id, c.name, s.code, s.name AS setname
				FROM card c
				LEFT JOIN card_set s ON (c.card_setid=s.id)
				WHERE c.name ILIKE %s
				ORDER BY c.name ASC, s.released DESC LIMIT 50"""
		results = fetch_query(qry, (search,))
		for r in results:
			r['iconurl'] = scryfall.get_set_icon(r['code'])

	return jsonify(results=results)


@app.route('/csv_upload', methods=['POST'])
@login_required
def csv_upload():
	import csv

	filename = '/tmp/upload_%s_%s.csv' % (os.urandom(32), session['userid'])
	request.files['upload'].save(filename)
	rows = []
	multiverse_ids = []
	with open(filename) as csvfile:
		importreader = csv.DictReader(csvfile)
		for row in importreader:
			rows.append(row)
			multiverse_ids.append(int(row['MultiverseID']))
	os.remove(filename)

	new = []
	for multiverseid in multiverse_ids:
		qry = "SELECT * FROM card WHERE multiverseid = %s"
		qargs = (multiverseid,)
		if len(fetch_query(qry, qargs)) == 0:
			new.append(multiverseid)

	bulk_lots = ([new[i:i + 75] for i in range(0, len(new), 75)])
	for lot in bulk_lots:
		resp = scryfall.get_bulk(lot)
		import_cards(resp)

	for row in rows:
		cardid = fetch_query("SELECT id FROM card WHERE multiverseid = %s", (row['MultiverseID'],), single_row=True)['id']
		collection.add(cardid, int(row['Foil quantity']) > 0, row['Quantity'])

	return jsonify(new)


def import_cards(cards):
	sets = []
	for c in cards:
		if c['set'] not in [x['code'] for x in sets]:
			sets.append({'code': c['set'], 'name': c['set_name']})

	for s in sets:
		# Check if already have a record of this set
		existing = fetch_query("SELECT 1 FROM card_set WHERE LOWER(code) = LOWER(%s)", (s['code'],))
		if not existing:
			resp = scryfall.get_set(s['code'])
			qry = """INSERT INTO card_set (name, code, released, tcgplayer_groupid) SELECT %s, %s, %s, %s
					WHERE NOT EXISTS (SELECT * FROM card_set WHERE code = %s)"""
			qargs = (resp['name'], s['code'], resp['released_at'], resp.get('tcgplayer_id'), s['code'],)
			mutate_query(qry, qargs)

	# more efficient than attempting inserts
	multiverse_ids = [x['multiverseid'] for x in fetch_query("SELECT multiverseid FROM card")]

	new_cards = []
	for c in cards:
		if c['multiverseid'] in multiverse_ids:
			print('Existing %s...' % c['multiverseid'])
			continue
		qry = """INSERT INTO card (
				collectornumber, multiverseid, name, card_setid, colors,
				rarity, multifaced) SELECT
				%s, %s, %s, (SELECT id FROM card_set WHERE code = %s), %s,
				%s, %s
				WHERE NOT EXISTS (
					SELECT 1 FROM card
					WHERE collectornumber = %s
					AND card_setid = (SELECT id FROM card_set WHERE code = %s))
				RETURNING id"""
		qargs = (
			c['collectornumber'], c['multiverseid'], c['name'], c['set'], c['colors'],
			c['rarity'], c['multifaced'],
			c['collectornumber'], c['set'],
		)
		new = mutate_query(qry, qargs, returning=True)
		if new:
			c['id'] = new['id']
			c['productid'] = tcgplayer.search(c)
			if c['productid'] is not None:
				mutate_query("UPDATE card SET tcgplayer_productid = %s WHERE id = %s", (c['productid'], c['id'],))
				new_cards.append({'id': c['id'], 'productid': c['productid']})

	bulk_lots = ([new_cards[i:i + 250] for i in range(0, len(new_cards), 250)])
	prices = {}
	for lot in bulk_lots:
		prices.update(tcgplayer.get_price({str(c['id']): str(c['productid']) for c in lot if c['productid'] is not None}))

	updates = []
	for cardid, price in prices.items():
		updates.append({'price': price['normal'], 'foilprice': price['foil'], 'id': cardid})
	mutate_query("UPDATE card SET price = %(price)s, foilprice = %(foilprice)s WHERE id = %(id)s", updates, executemany=True)


@app.route('/update_prices', methods=['GET'])
@check_celery_running
def update_prices():
	qry = """SELECT c.id, c.collectornumber, c.name, c.rarity,
				s.name AS set_name, s.tcgplayer_groupid AS groupid,
				c.tcgplayer_productid AS productid
			FROM card c
			LEFT JOIN card_set s ON (s.id = c.card_setid)
			WHERE EXISTS(SELECT 1 FROM user_card WHERE cardid=c.id)
			ORDER BY c.name ASC"""
	cards = fetch_query(qry)

	tcgplayer_token = tcgplayer.login()

	fetch_prices.delay(cards, tcgplayer_token)

	return jsonify()


@celery.task()
def fetch_prices(cards, tcgplayer_token):
	for c in cards:
		if c['productid'] is None:
			print('Searching for TCGPlayer ID for {} ({}).'.format(c['name'], c['set_name']))
			c['productid'] = tcgplayer.search(c, token=tcgplayer_token)
			if c['productid'] is not None:
				mutate_query("UPDATE card SET tcgplayer_productid = %s WHERE id = %s", (c['productid'], c['id'],))

	# Filter out cards without tcgplayerid to save requests
	cards = [c for c in cards if c['productid'] is not None]
	bulk_lots = ([cards[i:i + 250] for i in range(0, len(cards), 250)])
	prices = {}
	for lot in bulk_lots:
		prices.update(tcgplayer.get_price({str(c['id']): str(c['productid']) for c in lot if c['productid'] is not None}, token=tcgplayer_token))

	updates = []
	for cardid, price in prices.items():
		# Only update if we received have prices
		if price['normal'] is not None or price['foil'] is not None:
			updates.append({'price': price['normal'], 'foilprice': price['foil'], 'id': cardid})
	mutate_query("UPDATE card SET price = %(price)s, foilprice = %(foilprice)s WHERE id = %(id)s", updates, executemany=True)
	print('Updated prices for {} cards.'.format(len(updates)))


@app.route('/update_rates', methods=['POST'])
@check_celery_running
def update_rates():
	fetch_rates.delay()
	return jsonify()


@celery.task()
def fetch_rates():
	print('Fetching exchange rates')
	rates = openexchangerates.get()
	updates = [{'code': code, 'rate': rate} for code, rate in rates.items()]
	mutate_query("SELECT update_rates(%(code)s, %(rate)s)", updates, executemany=True)
	print('Updated exchange rates')


@app.route('/check_images', methods=['GET'])
@check_celery_running
def check_images():
	qry = """SELECT
				id, name, collectornumber, imageurl, arturl,
				(SELECT code FROM card_set WHERE id = card_setid)
			FROM card
			WHERE EXISTS(SELECT 1 FROM user_card WHERE cardid=card.id)
			ORDER BY name ASC"""
	cards = fetch_query(qry)

	for c in cards:
		check_card_image.delay(c)
		check_card_art.delay(c)

	return jsonify()


@celery.task()
def check_card_image(c):
	print('Checking card image for {}.'.format(c['name']))
	if c['imageurl'] is not None:
		# Check for bad image URLs
		if check_image_exists(c['imageurl']) is False:
			print('Card image URL for could not be found for {}.'.format(c['name']))
			mutate_query("""UPDATE card SET imageurl = NULL WHERE id = %s""", (c['id'],))
			# Null out local copy for refreshing image below
			c['imageurl'] = None

	if c['imageurl'] is None:
		# Fetch image URLs for anything without one
		c['imageurl'] = scryfall.get(c['code'], c['collectornumber'])['imageurl']
		if c['imageurl'] is not None:
			print('Found new card image URL for {}.'.format(c['name']))
			mutate_query("""UPDATE card SET imageurl = %s WHERE id = %s""", (c['imageurl'], c['id'],))


@celery.task()
def check_card_art(c):
	print('Checking card art for {}.'.format(c['name']))
	if c['arturl'] is not None:
		# Check for bad image URLs
		if check_image_exists(c['arturl']) is False:
			print('Card art URL for could not be found for {}.'.format(c['name']))
			mutate_query("""UPDATE card SET arturl = NULL WHERE id = %s""", (c['id'],))
			# Null out local copy for refreshing image below
			c['arturl'] = None

	if c['arturl'] is None:
		# Fetch image URLs for anything without one
		c['arturl'] = scryfall.get(c['code'], c['collectornumber'])['arturl']
		if c['arturl'] is not None:
			print('Found new card art URL for {}.'.format(c['name']))
			mutate_query("""UPDATE card SET arturl = %s WHERE id = %s""", (c['arturl'], c['id'],))


@app.route('/decks', methods=['GET'])
@login_required
def decks():
	return render_template('decks.html', active='decks')


@app.route('/decks/get/all', methods=['GET'])
@login_required
def decks_get_all():
	params = params_to_dict(request.args, bool_keys=['deleted'])
	results = deck.get_all(params['deleted'])
	return jsonify(results=results)


@app.route('/decks/get', methods=['GET'])
@login_required
def decks_get():
	params = params_to_dict(request.args)
	resp = {}
	resp['deck'] = deck.get(params['deckid'])
	resp['cards'] = deck.get_cards(params['deckid'])
	resp['formats'] = deck.get_formats()
	return jsonify(**resp)


@app.route('/decks/save', methods=['POST'])
@login_required
def decks_save():
	params = params_to_dict(request.form)
	qry = "UPDATE deck SET name = %s, formatid = %s WHERE id = %s AND userid = %s"
	qargs = (params['name'], params['formatid'], params['deckid'], session['userid'],)
	mutate_query(qry, qargs)
	return jsonify()


@app.route('/decks/delete', methods=['POST'])
@login_required
def decks_delete():
	params = params_to_dict(request.form)
	mutate_query("UPDATE deck SET deleted = true WHERE id = %s AND userid = %s", (params['deckid'], session['userid'],))
	return jsonify()


@app.route('/decks/restore', methods=['POST'])
@login_required
def decks_restore():
	params = params_to_dict(request.form)
	mutate_query("UPDATE deck SET deleted = false WHERE id = %s AND userid = %s", (params['deckid'], session['userid'],))
	return jsonify()


@app.route('/decks/import', methods=['POST'])
@login_required
def decks_import():
	import csv

	filename = '/tmp/upload_%s_%s.csv' % (os.urandom(32), session['userid'])
	request.files['upload'].save(filename)
	rows = []
	with open(filename) as csvfile:
		importreader = csv.DictReader(csvfile)
		for row in importreader:
			rows.append(row)
	os.remove(filename)

	qry = """INSERT INTO deck (name, userid, formatid)
			VALUES (concat('Imported Deck ', to_char(now(), 'YYYY-MM-DD HH12:MI:SS')), %s, (SELECT id FROM format WHERE name = 'Other'))
			RETURNING id"""
	deckid = mutate_query(qry, (session['userid'],), returning=True)['id']

	for row in rows:
		print(row)
		qry = """INSERT INTO deck_card (deckid, cardid, quantity, section)
				VALUES (%s, deck_card_match(%s), %s, %s)"""
		qargs = (deckid, row['Name'], row['Count'], row['Section'],)
		mutate_query(qry, qargs)

	qry = """UPDATE deck SET cardartid = (
				SELECT id FROM card WHERE EXISTS (
					SELECT 1 FROM deck_card WHERE cardid = card.id AND deckid = deck.id
				) ORDER BY random() LIMIT 1
			) WHERE id = %s"""
	mutate_query(qry, (deckid,))

	return jsonify()


if __name__ == '__main__':
	app.run()
