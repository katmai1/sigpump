"""
Wallets que compraron un token en los últimos minutos. Ver sigpump/buyers.py.

    python buyers.py <MINT> [--minutes 10] [--min-sol 2] [--min-age-days 180]
"""

from sigpump.buyers import main


if __name__ == "__main__":
    main()
