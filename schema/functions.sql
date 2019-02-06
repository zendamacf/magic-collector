
DROP FUNCTION IF EXISTS collector.get_collectornumber(INTEGER);
CREATE OR REPLACE FUNCTION collector.get_collectornumber(_cardid INTEGER) RETURNS TEXT AS $$
	SELECT CASE WHEN multifaced = true THEN
		concat(collectornumber, 'a')
	ELSE
		collectornumber
	END FROM card WHERE id = _cardid;
$$ LANGUAGE 'sql';

DROP FUNCTION IF EXISTS collector.get_price(INTEGER);
CREATE OR REPLACE FUNCTION collector.get_price(_user_cardid INTEGER) RETURNS MONEY AS $$
	SELECT CASE WHEN foil = true THEN
		foilprice
	ELSE
		price
	END * COALESCE((SELECT exchangerate FROM currency WHERE code = (SELECT currencycode FROM app.enduser WHERE id = userid)), 1)
	FROM card, user_card WHERE cardid = card.id AND user_card.id = _user_cardid;
$$ LANGUAGE 'sql';


DROP FUNCTION IF EXISTS collector.get_rarity(TEXT);
CREATE OR REPLACE FUNCTION collector.get_rarity(_initial TEXT) RETURNS TEXT AS $$
	SELECT CASE
	WHEN _initial = 'C' THEN 'Common'
	WHEN _initial = 'U' THEN 'Uncommon'
	WHEN _initial = 'R' THEN 'Rare'
	WHEN _initial = 'M' THEN 'Mythic'
	END;
$$ LANGUAGE 'sql';


DROP FUNCTION IF EXISTS collector.update_rates(TEXT, NUMERIC);
CREATE OR REPLACE FUNCTION collector.update_rates(_code TEXT, _exchangerate NUMERIC) RETURNS VOID AS $$
BEGIN
	IF EXISTS (SELECT * FROM currency WHERE UPPER(code) = UPPER(_code)) THEN
		UPDATE currency SET exchangerate = _exchangerate WHERE UPPER(code) = UPPER(_code);
	ELSE
		INSERT INTO currency (code, exchangerate) VALUES (UPPER(_code), _exchangerate);
	END IF;
	RETURN;
END;
$$ LANGUAGE 'plpgsql';


DROP FUNCTION IF EXISTS collector.deck_card_match(TEXT);
CREATE OR REPLACE FUNCTION collector.deck_card_match(_name TEXT) RETURNS INTEGER AS $$
DECLARE
	cardid INTEGER;
BEGIN
	SELECT c.id INTO cardid
		FROM card c
		LEFT JOIN card_set s ON (c.card_setid = s.id)
		WHERE LOWER(c.name) = LOWER(_name)
		ORDER BY s.released DESC LIMIT 1;

	-- If no matches, ILIKE for multifaced cards
	IF cardid IS NULL THEN
		SELECT c.id INTO cardid
			FROM card c
			LEFT JOIN card_set s ON (c.card_setid = s.id)
			WHERE c.multifaced AND c.name ILIKE concat('%', _name, '%')
			ORDER BY s.released DESC LIMIT 1;
	END IF;

	RETURN cardid;
END;
$$ LANGUAGE 'plpgsql';


DROP FUNCTION IF EXISTS collector.get_format(INTEGER);
CREATE OR REPLACE FUNCTION collector.get_format(_formatid INTEGER) RETURNS TEXT AS $$
	SELECT name FROM format WHERE id = _formatid;
$$ LANGUAGE 'sql';
