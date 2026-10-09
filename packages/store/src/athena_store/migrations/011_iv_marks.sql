CREATE TABLE iv_marks (
    symbol text NOT NULL,
    day date NOT NULL,
    credit numeric NOT NULL,
    underlying numeric NOT NULL,
    iv_proxy numeric NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, day)
);
