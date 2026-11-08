"""
Qué hace en la cadena cada wallet del fichero. Ver sigpump/wallet_audit.py.

    python wallet_audit.py [--txs 50] [--days 30] [--wallet DIRECCIÓN ...]
"""

from sigpump.wallet_audit import main


if __name__ == "__main__":
    main()
