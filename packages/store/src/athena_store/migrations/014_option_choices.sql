CREATE TABLE option_choices (
    symbol text NOT NULL,
    expiry date NOT NULL,
    contract_right text NOT NULL,
    strike numeric NOT NULL,
    PRIMARY KEY (symbol, expiry, contract_right, strike)
);
