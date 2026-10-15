"""Who may see what.

Two mechanisms, both enforced by the database rather than by application code:

- Record users (`DEFINE ACCESS account ... TYPE RECORD`): people who sign in
  and are then subject to the table PERMISSIONS in the schema. A record user
  on the payments team sees public documents plus the payments team's, and
  every query they run (vector search included) is filtered the same way.

- A VIEWER system user: a database login that can read everything but write
  nothing. Text-to-SurrealQL executes generated queries with it, so even a
  query that slips past the static checks cannot change data. "Everything"
  includes password hashes and every user's memories, which is why
  text2surql.check_query also refuses the tables it keeps out of the prompt.
"""

from __future__ import annotations

import secrets

from contextlayer.config import Settings
from contextlayer.db import Database, rid_str


def create_user(db: Database, *, email: str, name: str, password: str, teams: list[str]) -> str:
    """Create or update a record user. Requires a privileged connection."""
    result = db.query(
        """
        LET $existing = (SELECT VALUE id FROM user WHERE email = $email)[0];
        IF $existing {
            RETURN (UPDATE ONLY $existing SET name = $name, teams = $teams,
                    password = crypto::argon2::generate($password)).id;
        } ELSE {
            RETURN (CREATE ONLY user SET email = $email, name = $name, teams = $teams,
                    password = crypto::argon2::generate($password)).id;
        };
        """,
        {"email": email, "name": name, "password": password, "teams": teams},
    )
    return rid_str(result[-1])


def connect_as_user(email: str, password: str, settings: Settings | None = None) -> Database:
    """A connection that acts as a record user: every query is filtered by PERMISSIONS."""
    db = Database.connect(settings, signin=False)
    db.signin_record("account", email=email, password=password)
    return db


def ensure_viewer(db: Database, username: str = "ctx_reader", password: str | None = None) -> tuple[str, str]:
    """Define (or redefine) a read-only database user. Returns (username, password).

    The password comes from the argument, then CL_VIEWER_PASS, then a random one.
    Redefining the user doesn't end sessions already signed in, but a process
    that signs in after another one changed the password is refused. With
    CL_VIEWER_PASS set, every process defines the same password and none can
    lock another out.
    """
    if not username.isidentifier():
        raise ValueError("username must be a plain identifier")
    password = password or db.settings.viewer_pass or secrets.token_urlsafe(24)
    # DEFINE USER takes a literal, not a parameter; quote it as a SurrealQL string.
    literal = password.replace("\\", "\\\\").replace('"', '\\"')
    db.query(f'DEFINE USER OVERWRITE {username} ON DATABASE PASSWORD "{literal}" ROLES VIEWER')
    return username, password
