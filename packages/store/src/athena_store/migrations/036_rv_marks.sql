CREATE TABLE rv_marks (
    symbol text NOT NULL,
    day date NOT NULL,
    rv20 numeric NOT NULL,
    method text NOT NULL DEFAULT 'close_log_252',
    PRIMARY KEY (symbol, day)
);
