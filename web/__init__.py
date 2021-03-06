# Standard library imports
import os

# Third party imports
from flask import (
	request, session, jsonify, send_from_directory, flash, redirect, url_for,
	render_template, Flask, Response, got_request_exception
)
import rollbar
import rollbar.contrib.flask

# Local imports
from web import (
	collection, deck, scryfall, tcgplayer, config,
	functions
)
from flasktools import handle_exception, params_to_dict, serve_static_file
from flasktools.auth import is_logged_in, check_login, login_required
from flasktools.db import disconnect_database, fetch_query, mutate_query

app = Flask(__name__)

app.secret_key = config.SECRETKEY

app.jinja_env.globals.update(is_logged_in=is_logged_in)
app.jinja_env.globals.update(static_file=serve_static_file)


@app.before_first_request
def init_rollbar():
	if not hasattr(config, 'TESTMODE'):
		env = 'production'
		if request.remote_addr == '127.0.0.1':
			env = 'development'
		rollbar.init(
			config.ROLLBAR_TOKEN,
			environment=env
		)

		# send exceptions from `app` to rollbar, using flask's signal system.
		got_request_exception.connect(rollbar.contrib.flask.report_exception, app)


# Import below app initialisation
from web import asynchro


@app.errorhandler(500)
def internal_error(e: Exception) -> Response:
	return handle_exception()


@app.teardown_appcontext
def teardown(e: Exception) -> Response:
	disconnect_database()


@app.route('/ping')
def ping() -> Response:
	return jsonify(ping='pong')


@app.route('/favicon.ico')
@app.route('/robots.txt')
@app.route('/sitemap.xml')
@app.route('/search.xml')
def static_from_root() -> Response:
	return send_from_directory(app.static_folder, request.path[1:])


@app.route('/login', methods=['GET', 'POST'])
def login() -> Response:
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
def logout() -> Response:
	session.pop('userid', None)
	return redirect(url_for('login'))


@app.route('/', methods=['GET'])
@login_required
def home() -> Response:
	return render_template('collection.html', active='collection')


@app.route('/get_sets', methods=['GET'])
@login_required
def get_sets() -> Response:
	sets = fetch_query(
		"SELECT id, name, code FROM card_set ORDER BY released DESC"
	)
	for s in sets:
		if not os.path.exists(asynchro.set_icon_filename(s['code'])):
			asynchro.get_set_icon.delay(s['code'])
		s['iconurl'] = serve_static_file('images/set_icon_{}.svg'.format(s['code']))

	return jsonify(sets=sets)


@app.route('/get_collection', methods=['GET'])
@login_required
def get_collection() -> Response:
	params = params_to_dict(request.args)
	resp = collection.get(params)
	for c in resp['cards']:
		if not os.path.exists(asynchro.card_art_filename(c['id'])):
			asynchro.get_card_art.delay(c['id'], c['setcode'], c['collectornumber'])
		if not os.path.exists(asynchro.card_image_filename(c['id'])):
			asynchro.get_card_image.delay(c['id'], c['setcode'], c['collectornumber'])
		del c['id']
		del c['collectornumber']

	return jsonify(**resp)


@app.route('/collection/card', methods=['GET'])
@login_required
def collection_card() -> Response:
	params = params_to_dict(request.args)
	resp = {'card': None}

	if params.get('user_cardid'):
		resp['card'] = fetch_query(
			"""
			SELECT
				p.id, c.name, cs.name AS setname, get_rarity(p.rarity) AS rarity,
				uc.quantity, uc.foil, get_price(uc.id) AS price, p.tcgplayer_productid,
				COALESCE(
					(SELECT currencycode FROM app.enduser WHERE id = uc.userid),
					'USD'
				) AS currencycode,
				total_printings_owned(uc.userid, p.cardid) AS printingsowned,
				(
					SELECT to_char(MAX(created), 'DD/MM/YY')
					FROM price_history
					WHERE printingid = p.id
				) AS price_lastupdated,
				CASE WHEN p.language != 'en' THEN UPPER(p.language) END AS language
			FROM user_card uc
			LEFT JOIN printing p ON (uc.printingid = p.id)
			LEFT JOIN card c ON (p.cardid = c.id)
			LEFT JOIN card_set cs ON (p.card_setid = cs.id)
			WHERE uc.userid = %s
			AND uc.id = %s
			""",
			(session['userid'], params['user_cardid'],),
			single_row=True
		)

	if resp['card']:
		cardid = resp['card']['id']
		resp['card']['arturl'] = serve_static_file(f"images/card_art_{cardid}.jpg")
		resp['card']['decks'] = fetch_query(
			"""
			SELECT
				d.name, get_format(d.formatid) AS formatname,
				SUM(dc.quantity) AS quantity,
				d.cardartid
			FROM deck_card dc
			LEFT JOIN deck d ON (d.id = dc.deckid)
			WHERE d.deleted = false
			AND d.userid = %s
			AND dc.cardid IN (SELECT cardid FROM printing WHERE id = %s)
			GROUP BY d.id
			ORDER BY d.formatid, d.name
			""",
			(session['userid'], cardid,)
		)
		for d in resp['card']['decks']:
			d['arturl'] = serve_static_file(f"images/card_art_{d['cardartid']}.jpg")
	else:
		resp['error'] = 'No card selected.'

	return jsonify(**resp)


@app.route('/collection/card/pricerefresh', methods=['GET'])
@login_required
def collection_card_pricerefresh() -> Response:
	params = params_to_dict(request.args)

	printingid = None
	if params.get('user_cardid'):
		printingid = fetch_query(
			"SELECT printingid FROM user_card WHERE id = %s",
			(params['user_cardid'],),
			single_row=True
		)['printingid']

	if printingid is not None:
		return update_prices(printingid=printingid)

	return jsonify(error='No card found.')


@app.route('/collection/card/pricehistory', methods=['GET'])
@login_required
def collection_card_pricehistory() -> Response:
	params = params_to_dict(request.args)
	resp = {}

	printingid = None
	if params.get('user_cardid'):
		printingid = fetch_query(
			"SELECT printingid FROM user_card WHERE id = %s",
			(params['user_cardid'],),
			single_row=True
		)['printingid']

	if printingid is not None:
		history = fetch_query(
			"""
			SELECT
				convert_price(ph.price, %s)::NUMERIC AS price,
				convert_price(ph.foilprice, %s)::NUMERIC AS foilprice,
				to_char(d.day, 'DD/MM/YY') AS created
			FROM generate_series(
				(SELECT MIN(created) FROM price_history WHERE printingid = %s),
				(SELECT MAX(created) FROM price_history WHERE printingid = %s),
				'1 day'::INTERVAL
			) d(day)
			LEFT JOIN price_history ph ON (ph.created = d.day AND ph.printingid = %s)
			""",
			(session['userid'], session['userid'], printingid, printingid, printingid,)
		)

		resp['dates'] = [h['created'] for h in history]
		prices = {
			'label': 'Price',
			'backgroundColor': 'rgba(40, 181, 246, 0.2)',
			'borderColor': 'rgba(40, 181, 246, 1)',
			'data': [functions.make_float(h['price']) for h in history]
		}
		foilprices = {
			'label': 'Foil Price',
			'backgroundColor': 'rgba(175, 90, 144, 0.2)',
			'borderColor': 'rgba(175, 90, 144, 1)',
			'data': [functions.make_float(h['foilprice']) for h in history]
		}

		resp['datasets'] = []
		if len(prices['data']) > 0:
			resp['datasets'].append(prices)
		if len(foilprices['data']) > 0:
			resp['datasets'].append(foilprices)
	else:
		resp['error'] = 'No card selected.'

	return jsonify(**resp)


@app.route('/collection/card/add', methods=['POST'])
@login_required
def collection_card_add() -> Response:
	params = params_to_dict(request.form, bool_keys=['foil'])
	resp = {}

	if params.get('printingid'):
		collection.add(params['printingid'], params['foil'], params['quantity'])
	else:
		resp['error'] = 'No card selected.'

	return jsonify(**resp)


@app.route('/collection/card/edit', methods=['POST'])
@login_required
def collection_card_edit() -> Response:
	params = params_to_dict(request.form, bool_keys=['foil'])

	update_current = True
	existing = fetch_query(
		"SELECT printingid, foil FROM user_card WHERE id = %s AND userid = %s",
		(params['user_cardid'], session['userid'],),
		single_row=True
	)
	if params.get('tcgplayer_productid'):
		print('Updating TCGplayer ID')
		mutate_query(
			"""
			UPDATE printing SET tcgplayer_productid = %s
			WHERE id = %s
			AND tcgplayer_productid IS NULL
			""",
			(params['tcgplayer_productid'], existing['printingid'],)
		)
	if existing['foil'] != params['foil']:
		# Foil has changed, need to check for opposite record
		opposite = fetch_query(
			"""
			SELECT * FROM user_card WHERE printingid = %s AND userid = %s AND foil != %s
			""",
			(existing['printingid'], session['userid'], existing['foil'],),
			single_row=True
		)
		if opposite:
			# There's an opposite record, update this instead
			mutate_query(
				"UPDATE user_card SET quantity = quantity + %s WHERE id = %s",
				(params['quantity'], opposite['id'],)
			)
			mutate_query(
				"DELETE FROM user_card WHERE id = %s",
				(params['user_cardid'],)
			)
			update_current = False

	if (update_current):
		if int(params['quantity']) > 0:
			mutate_query(
				"""
				UPDATE user_card SET quantity = %s, foil = %s WHERE id = %s AND userid = %s
				""",
				(
					params['quantity'],
					params['foil'],
					params['user_cardid'],
					session['userid'],
				)
			)
		else:
			mutate_query(
				"DELETE FROM user_card WHERE id = %s AND userid = %s",
				(params['user_cardid'], session['userid'],)
			)

	return jsonify()


@app.route('/search', methods=['GET'])
@login_required
def search() -> Response:
	params = params_to_dict(request.args)
	results = []

	if params.get('query'):
		search = '%' + params['query'] + '%'
		results = fetch_query(
			"""
			SELECT
				p.id, c.name, s.code, s.name AS setname, s.code AS setcode,
				CASE WHEN p.language != 'en' THEN UPPER(p.language) END AS language,
				p.collectornumber
			FROM printing p
			LEFT JOIN card c ON (p.cardid = c.id)
			LEFT JOIN card_set s ON (p.card_setid = s.id)
			WHERE c.name ILIKE %s
			ORDER BY c.name ASC, s.released DESC LIMIT 50
			""",
			(search,)
		)
		for r in results:
			if not os.path.exists(asynchro.card_image_filename(r['id'])):
				asynchro.get_card_image.delay(r['id'], r['setcode'], r['collectornumber'])
			r['imageurl'] = serve_static_file(f"images/card_image_{r['id']}.jpg")

	return jsonify(results=results)


@app.route('/csv_upload', methods=['POST'])
@login_required
def csv_upload() -> Response:
	import csv

	upload = request.files['upload']
	filename = '/tmp/upload_{}_{}.csv'.format(os.urandom(32), session['userid'])
	try:
		upload.save(filename)
	except FileNotFoundError:
		return jsonify(error='Error uploading file. Please try again.')
	rows = []
	scryfall_ids = []
	with open(filename) as csvfile:
		importreader = csv.DictReader(csvfile)
		for row in importreader:
			rows.append({
				'scryfallid': row['Scryfall ID'],
				'foil': int(row['Foil quantity']) > 0,
				'quantity': row['Quantity']
			})
			scryfall_ids.append(row['Scryfall ID'])
	os.remove(filename)

	new = []
	for scryfallid in scryfall_ids:
		qry = "SELECT 1 FROM printing WHERE scryfallid = %s::TEXT"
		qargs = (scryfallid,)
		if len(fetch_query(qry, qargs)) == 0:
			new.append(scryfallid)

	bulk_lots = ([new[i:i + 75] for i in range(0, len(new), 75)])
	for lot in bulk_lots:
		resp = scryfall.get_bulk(lot)
		collection.import_cards(resp)

	importid = mutate_query(
		"""
		INSERT INTO import (filename, userid)
		VALUES (%s, %s)
		RETURNING id
		""",
		(upload.filename, session['userid'],),
		returning=True
	)['id']

	for row in rows:
		row['printingid'] = fetch_query(
			"SELECT id FROM printing WHERE scryfallid = %s::TEXT",
			(row['scryfallid'],),
			single_row=True
		)['id']
		# Doing this in loop instead of executemany due to needing RETURNING
		row['import_rowid'] = mutate_query(
			"""
			INSERT INTO import_row (importid, printingid, foil, quantity)
			VALUES (%s, %s, %s, %s)
			RETURNING id
			""",
			(importid, row['printingid'], row['foil'], row['quantity'],),
			returning=True
		)['id']

	complete_import(importid)

	return jsonify(new)


def complete_import(importid: int) -> None:
	rows = fetch_query(
		"SELECT * FROM import_row WHERE NOT complete AND importid = %s",
		(importid,)
	)
	for row in rows:
		collection.add(row['printingid'], row['foil'], row['quantity'])
		# Mark import for this card as completed
		mutate_query(
			"UPDATE import_row SET complete = true WHERE id = %s",
			(row['id'],)
		)


@app.route('/update_prices', methods=['GET'])
@app.route('/update_prices/<int:printingid>', methods=['GET'])
def update_prices(printingid: int = None) -> Response:
	_update_prices(printingid=printingid)

	return jsonify()


@app.route('/update_prices/missing', methods=['GET'])
def update_missing_prices() -> Response:
	_update_prices(missing_prices=True)

	return jsonify()


def _update_prices(printingid=None, missing_prices=False):
	qry = """SELECT p.id, p.collectornumber, c.name, p.rarity,
				s.code AS set_code, s.name AS set_name, s.tcgplayer_groupid AS groupid,
				p.tcgplayer_productid AS productid
			FROM printing p
			LEFT JOIN card_set s ON (s.id = p.card_setid)
			LEFT JOIN card c ON (c.id = p.cardid)
			WHERE NOT is_basic_land(c.id)"""
	qargs = ()
	if printingid is not None:
		qry += " AND p.id = %s"
		qargs += (printingid,)
	if missing_prices:
		qry += " AND COALESCE(p.price, p.foilprice) IS NULL"
	qry += """ ORDER BY
		EXISTS(SELECT 1 FROM user_card WHERE printingid=c.id) DESC,
		c.name ASC"""
	cards = fetch_query(qry, qargs)

	tcgplayer_token = tcgplayer.login()

	asynchro.fetch_prices.delay(cards, tcgplayer_token)


@app.route('/update_rates', methods=['POST'])
def update_rates() -> Response:
	asynchro.fetch_rates.delay()
	return jsonify()


@app.route('/refresh', methods=['POST'])
@login_required
def refresh() -> Response:
	params = params_to_dict(request.form)
	asynchro.refresh_from_scryfall.delay(params['query'])
	return jsonify()


@app.route('/decks', methods=['GET'])
@login_required
def decks() -> Response:
	return render_template('decks.html', active='decks')


@app.route('/decks/<int:deckid>', methods=['GET'])
@login_required
def decklist(deckid: int) -> Response:
	formats = deck.get_formats()
	return render_template('decklist.html', deckid=deckid, formats=formats)


@app.route('/decks/get/all', methods=['GET'])
@login_required
def decks_get_all() -> Response:
	params = params_to_dict(request.args, bool_keys=['deleted'])
	results = deck.get_all(params['deleted'])
	for r in results:
		if r['cardid']:
			if not os.path.exists(asynchro.card_art_filename(r['cardid'])):
				asynchro.get_card_art.delay(r['cardid'], r['code'], r['collectornumber'])
			r['arturl'] = serve_static_file(f"images/card_art_{r['cardid']}.jpg")
			del r['code']
			del r['collectornumber']

		r['viewurl'] = url_for('decklist', deckid=r['id'])
		del r['cardid']

	return jsonify(results=results)


@app.route('/decks/get', methods=['GET'])
@login_required
def decks_get() -> Response:
	params = params_to_dict(request.args)
	resp = {}
	resp['deck'] = deck.get(params['deckid'])
	resp['main'], resp['sideboard'] = deck.get_cards(params['deckid'])

	if not os.path.exists(asynchro.card_art_filename(resp['deck']['cardid'])):
		asynchro.get_card_art.delay(
			resp['deck']['cardid'],
			resp['deck']['code'],
			resp['deck']['collectornumber']
		)
	resp['deck']['arturl'] = serve_static_file(
		f"images/card_art_{resp['deck']['cardid']}.jpg"
	)
	del resp['deck']['cardid']
	del resp['deck']['code']
	del resp['deck']['collectornumber']

	resp['main'] = deck.parse_types(resp['main'])
	resp['sideboard'] = deck.parse_types(resp['sideboard'])

	return jsonify(**resp)


@app.route('/decks/save', methods=['POST'])
@login_required
def decks_save() -> Response:
	params = params_to_dict(request.form)
	mutate_query(
		"""
		UPDATE
			deck
		SET
			name = %s,
			formatid = %s,
			notes = %s
		WHERE
			id = %s AND
			userid = %s""",
		(
			params['name'],
			params['formatid'],
			params['notes'],
			params['deckid'],
			session['userid'],
		)
	)
	return jsonify()


@app.route('/decks/delete', methods=['POST'])
@login_required
def decks_delete() -> Response:
	params = params_to_dict(request.form)
	mutate_query(
		"UPDATE deck SET deleted = true WHERE id = %s AND userid = %s",
		(params['deckid'], session['userid'],)
	)
	return jsonify()


@app.route('/decks/restore', methods=['POST'])
@login_required
def decks_restore() -> Response:
	params = params_to_dict(request.form)
	mutate_query(
		"UPDATE deck SET deleted = false WHERE id = %s AND userid = %s",
		(params['deckid'], session['userid'],)
	)
	return jsonify()


@app.route('/decks/cardart', methods=['POST'])
@login_required
def decks_set_cardart() -> Response:
	params = params_to_dict(request.form)
	mutate_query(
		"UPDATE deck SET cardartid = %s WHERE id = %s AND userid = %s",
		(params['cardid'], params['deckid'], session['userid'],)
	)
	return jsonify()


@app.route('/decks/cards/delete', methods=['POST'])
@login_required
def decks_cards_delete() -> Response:
	params = params_to_dict(request.form)
	mutate_query(
		"""
		DELETE FROM deck_card
		WHERE id = %s
		AND (SELECT userid FROM deck WHERE deck.id = deckid) = %s
		""",
		(params['deck_cardid'], session['userid'],)
	)
	return jsonify()


@app.route('/decks/import/csv', methods=['POST'])
@login_required
def decks_import_csv() -> Response:
	import csv

	params = params_to_dict(request.form)
	filename = '/tmp/upload_{}_{}.csv'.format(os.urandom(32), session['userid'])
	try:
		request.files['upload'].save(filename)
	except FileNotFoundError:
		return jsonify(error='Error uploading file. Please try again.')
	rows = []
	with open(filename) as csvfile:
		importreader = csv.DictReader(csvfile)
		for row in importreader:
			rows.append(row)
	os.remove(filename)

	cards = []
	for r in rows:
		card = {
			'name': r['Name'],
			'quantity': r['Count'],
			'section': 'main' if row['Section'] == 'main' else 'sideboard'
		}
		cards.append(card)

	deck.do_import(params['name'], cards)

	return jsonify()


@app.route('/decks/import/arena', methods=['POST'])
@login_required
def decks_import_arena() -> Response:
	import re

	params = params_to_dict(request.form)
	main, sideboard = params['import'].split('\n\n')
	cards = []
	notes = ''

	# Flake-8 doesn't like lambdas as functions!
	def parse_row(row):
		regex_parsers = [
			r"^([\d]+)([\w\s',/-]+)\(.+\)\s\d+$",
			r"^([\d]+)([\w\s',/-]+)\(.+\)$",
			r"^([\d]+)([\w\s',/-]+)$"
		]
		for parser in regex_parsers:
			match = re.match(parser, row)
			if match:
				return match

	def populate_card(match, section):
		return {
			'name': match.group(2).strip(),
			'quantity': match.group(1).strip(),
			'section': section
		}

	for row in main.split('\n'):
		match = parse_row(row)
		if match:
			card = populate_card(match, 'main')
			cards.append(card)
		else:
			notes += "Couldn't import: {}\n".format(row)

	for row in sideboard.split('\n'):
		match = parse_row(row)
		if match:
			card = populate_card(match, 'sideboard')
			cards.append(card)
		else:
			notes += "Couldn't import sideboard: {}\n".format(row)

	if notes == '':
		notes = None

	deck.do_import(params['name'], cards, notes=notes)

	return jsonify()


if __name__ == '__main__':
	app.run()
