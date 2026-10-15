-- =====================================================================
-- Context layer schema (SurrealDB 3.x)
--
-- {{EMBEDDING_DIM}} is replaced by the embedder's dimension at apply time.
-- Every statement is idempotent: re-running the file is safe.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Which embedding model filled this database
-- ---------------------------------------------------------------------
-- Vectors from different models can't be compared, and two models with the
-- same dimension mix without any error. meta:embedding records the model so
-- the code can refuse a mismatch instead of returning noise.
DEFINE TABLE OVERWRITE meta SCHEMALESS
    PERMISSIONS FOR select FULL FOR create, update, delete NONE;


-- ---------------------------------------------------------------------
-- Text analysis for BM25 full-text search
-- ---------------------------------------------------------------------
-- `class` splits on character-class changes (letters/digits/punctuation),
-- so "ERR_LEDGER_409" becomes ERR, LEDGER, 409 and still matches exactly.
DEFINE ANALYZER OVERWRITE cl_english
    TOKENIZERS class, blank
    FILTERS lowercase, ascii, snowball(english);


-- ---------------------------------------------------------------------
-- Users and record-level access
-- ---------------------------------------------------------------------
-- Users are provisioned by an admin (no SIGNUP clause), because a user
-- choosing their own `teams` would let them read any team's documents.
DEFINE TABLE OVERWRITE user SCHEMAFULL
    PERMISSIONS
        FOR select WHERE id = $auth.id
        FOR create, update, delete NONE;
DEFINE FIELD OVERWRITE email    ON user TYPE string ASSERT string::is_email($value);
DEFINE FIELD OVERWRITE name     ON user TYPE string;
DEFINE FIELD OVERWRITE teams    ON user TYPE array<string> DEFAULT [];
DEFINE FIELD OVERWRITE password ON user TYPE string PERMISSIONS FOR select NONE;
DEFINE INDEX IF NOT EXISTS user_email ON user FIELDS email UNIQUE;

DEFINE ACCESS OVERWRITE account ON DATABASE TYPE RECORD
    SIGNIN (
        SELECT * FROM user
        WHERE email = $email AND crypto::argon2::compare(password, $password)
    )
    DURATION FOR TOKEN 15m, FOR SESSION 12h;


-- ---------------------------------------------------------------------
-- Documents: the unit a person thinks in (a page, a runbook, a ticket)
-- ---------------------------------------------------------------------
DEFINE TABLE OVERWRITE document SCHEMAFULL
    PERMISSIONS
        FOR select WHERE
            visibility = 'public'
            OR (visibility = 'team' AND team IN $auth.teams)
            OR owner = $auth.id
        FOR create, update, delete NONE;
DEFINE FIELD OVERWRITE title      ON document TYPE string;
DEFINE FIELD OVERWRITE content    ON document TYPE string;
DEFINE FIELD OVERWRITE kind       ON document TYPE string DEFAULT 'note';
DEFINE FIELD OVERWRITE tags       ON document TYPE array<string> DEFAULT [];
DEFINE FIELD OVERWRITE source     ON document TYPE option<string>;
DEFINE FIELD OVERWRITE visibility ON document TYPE string DEFAULT 'public'
    ASSERT $value IN ['public', 'team', 'private'];
DEFINE FIELD OVERWRITE team       ON document TYPE option<string>;
DEFINE FIELD OVERWRITE owner      ON document TYPE option<record<user>>;
DEFINE FIELD OVERWRITE metadata   ON document TYPE object FLEXIBLE DEFAULT {};
-- `$before` keeps the original timestamp when UPSERT ... CONTENT replaces the record.
DEFINE FIELD OVERWRITE created_at ON document TYPE datetime VALUE $before OR time::now();
DEFINE FIELD OVERWRITE updated_at ON document TYPE datetime VALUE time::now();
DEFINE INDEX IF NOT EXISTS document_kind ON document FIELDS kind;


-- ---------------------------------------------------------------------
-- Chunks: the unit a retriever thinks in
-- ---------------------------------------------------------------------
-- Several chunking strategies can live side by side; `strategy` tells
-- them apart. A `child` chunk points at its `parent` so you can retrieve
-- something small and hand the model something large.
DEFINE TABLE OVERWRITE chunk SCHEMAFULL
    PERMISSIONS
        FOR select WHERE
            document.visibility = 'public'
            OR (document.visibility = 'team' AND document.team IN $auth.teams)
            OR document.owner = $auth.id
        FOR create, update, delete NONE;
DEFINE FIELD OVERWRITE document    ON chunk TYPE record<document> REFERENCE ON DELETE CASCADE;
DEFINE FIELD OVERWRITE parent      ON chunk TYPE option<record<chunk>>;
DEFINE FIELD OVERWRITE strategy    ON chunk TYPE string;
DEFINE FIELD OVERWRITE position    ON chunk TYPE int;
DEFINE FIELD OVERWRITE heading     ON chunk TYPE option<string>;
DEFINE FIELD OVERWRITE text        ON chunk TYPE string;
DEFINE FIELD OVERWRITE token_count ON chunk TYPE int;
DEFINE FIELD OVERWRITE embedding   ON chunk TYPE option<array<float>>;

DEFINE INDEX IF NOT EXISTS chunk_document ON chunk FIELDS document;
DEFINE INDEX IF NOT EXISTS chunk_strategy ON chunk FIELDS strategy;
DEFINE INDEX IF NOT EXISTS chunk_embedding ON chunk FIELDS embedding
    HNSW DIMENSION {{EMBEDDING_DIM}} DIST COSINE EFC 150 M 12;
DEFINE INDEX IF NOT EXISTS chunk_text ON chunk FIELDS text
    FULLTEXT ANALYZER cl_english BM25(1.2, 0.75) HIGHLIGHTS;


-- ---------------------------------------------------------------------
-- The document graph: explicit links between documents
-- ---------------------------------------------------------------------
-- document:incident-x ->links_to-> document:ledger-service
DEFINE TABLE OVERWRITE links_to TYPE RELATION IN document OUT document SCHEMAFULL
    PERMISSIONS
        FOR select WHERE
            out.visibility = 'public'
            OR (out.visibility = 'team' AND out.team IN $auth.teams)
            OR out.owner = $auth.id
        FOR create, update, delete NONE;
DEFINE FIELD OVERWRITE anchor ON links_to TYPE option<string>;
DEFINE FIELD OVERWRITE kind   ON links_to TYPE string DEFAULT 'wikilink';
DEFINE INDEX IF NOT EXISTS links_to_unique ON links_to FIELDS in, out UNIQUE;


-- ---------------------------------------------------------------------
-- Agent memory
-- ---------------------------------------------------------------------
-- What an agent has learned, where it learned it, how often it has been
-- useful, and when it should be forgotten.
--
-- Facts are never overwritten. A correction creates a new memory, ends the
-- old one's valid time, and links the two with `supersedes`. Two clocks:
--
--   valid time   [valid_from, valid_until)  when the fact was true in the world
--   record time  created_at, superseded_at  when we learned the fact, and when
--                                            we learned that it had ended
--
-- so "where did Pat work on 1 April?" (valid time) and "what did the agent
-- believe on 1 April?" (record time) are different questions, both answerable.
DEFINE TABLE OVERWRITE memory SCHEMAFULL
    PERMISSIONS
        FOR select, create, update, delete WHERE owner = $auth.id;
DEFINE FIELD OVERWRITE agent         ON memory TYPE string;
DEFINE FIELD OVERWRITE owner         ON memory TYPE option<record<user>>;
DEFINE FIELD OVERWRITE kind          ON memory TYPE string DEFAULT 'fact'
    ASSERT $value IN ['fact', 'preference', 'episode', 'instruction'];
DEFINE FIELD OVERWRITE content       ON memory TYPE string;
-- Optional slot: (subject, attribute) such as ('user', 'team'). A slot holds
-- one current value, so a new memory in an occupied slot supersedes the old.
DEFINE FIELD OVERWRITE subject       ON memory TYPE option<string>;
DEFINE FIELD OVERWRITE attribute     ON memory TYPE option<string>;
DEFINE FIELD OVERWRITE importance    ON memory TYPE float DEFAULT 0.5
    ASSERT $value >= 0 AND $value <= 1;
DEFINE FIELD OVERWRITE embedding     ON memory TYPE array<float>;
DEFINE FIELD OVERWRITE created_at    ON memory TYPE datetime DEFAULT time::now() READONLY;
DEFINE FIELD OVERWRITE valid_from    ON memory TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE valid_until   ON memory TYPE option<datetime>;
DEFINE FIELD OVERWRITE superseded_at ON memory TYPE option<datetime>;
DEFINE FIELD OVERWRITE last_accessed ON memory TYPE datetime DEFAULT time::now();
DEFINE FIELD OVERWRITE access_count  ON memory TYPE int DEFAULT 0;
DEFINE FIELD OVERWRITE expires_at    ON memory TYPE option<datetime>;
-- Provenance: where the memory came from and how sure we are of it.
DEFINE FIELD OVERWRITE source        ON memory TYPE option<string>;
DEFINE FIELD OVERWRITE source_turn   ON memory TYPE option<record<turn>>;
DEFINE FIELD OVERWRITE confidence    ON memory TYPE float DEFAULT 1.0
    ASSERT $value >= 0 AND $value <= 1;

-- Recall filters to one agent and one user first, then compares vectors
-- exactly; that scope is what needs an index, not the embedding.
DEFINE INDEX IF NOT EXISTS memory_scope ON memory FIELDS agent, owner;
DEFINE INDEX IF NOT EXISTS memory_slot ON memory FIELDS agent, owner, subject, attribute;

-- memory:new ->supersedes-> memory:old
DEFINE TABLE OVERWRITE supersedes TYPE RELATION IN memory OUT memory SCHEMAFULL
    PERMISSIONS
        FOR select WHERE in.owner = $auth.id
        FOR create WHERE in.owner = $auth.id AND out.owner = $auth.id
        FOR update, delete NONE;
DEFINE FIELD OVERWRITE reason ON supersedes TYPE string DEFAULT 'correction';
DEFINE FIELD OVERWRITE at     ON supersedes TYPE datetime DEFAULT time::now() READONLY;

-- The conversation log memories are extracted from: provenance points here.
DEFINE TABLE OVERWRITE turn SCHEMAFULL
    PERMISSIONS
        FOR select, create, delete WHERE owner = $auth.id
        FOR update NONE;
DEFINE FIELD OVERWRITE agent      ON turn TYPE string;
DEFINE FIELD OVERWRITE owner      ON turn TYPE option<record<user>>;
DEFINE FIELD OVERWRITE session    ON turn TYPE string;
DEFINE FIELD OVERWRITE role       ON turn TYPE string ASSERT $value IN ['user', 'assistant'];
DEFINE FIELD OVERWRITE content    ON turn TYPE string;
DEFINE FIELD OVERWRITE created_at ON turn TYPE datetime DEFAULT time::now() READONLY;
DEFINE INDEX IF NOT EXISTS turn_session ON turn FIELDS agent, owner, session, created_at;

-- memory:abc ->about-> document:ledger-service
DEFINE TABLE OVERWRITE about TYPE RELATION IN memory OUT document SCHEMAFULL
    PERMISSIONS FOR select WHERE in.owner = $auth.id;
DEFINE INDEX IF NOT EXISTS about_unique ON about FIELDS in, out UNIQUE;


-- ---------------------------------------------------------------------
-- Text-to-SurrealQL: worked examples, retrieved by similarity
-- ---------------------------------------------------------------------
DEFINE TABLE OVERWRITE surql_example SCHEMAFULL
    PERMISSIONS FOR select FULL FOR create, update, delete NONE;
DEFINE FIELD OVERWRITE question  ON surql_example TYPE string;
DEFINE FIELD OVERWRITE query     ON surql_example TYPE string;
DEFINE FIELD OVERWRITE embedding ON surql_example TYPE array<float>;
DEFINE INDEX IF NOT EXISTS surql_example_embedding ON surql_example FIELDS embedding
    HNSW DIMENSION {{EMBEDDING_DIM}} DIST COSINE;


-- ---------------------------------------------------------------------
-- Retrieval metrics as database functions
-- ---------------------------------------------------------------------
-- $retrieved: ranked ids the retriever returned; $relevant: labelled ids.
DEFINE FUNCTION OVERWRITE fn::recall_at_k($retrieved: array, $relevant: array, $k: int) -> float {
    IF array::len($relevant) = 0 { RETURN 0.0; };
    LET $top = array::slice($retrieved, 0, $k);
    RETURN <float> array::len(array::intersect($top, $relevant)) / array::len($relevant);
};

DEFINE FUNCTION OVERWRITE fn::precision_at_k($retrieved: array, $relevant: array, $k: int) -> float {
    IF $k = 0 { RETURN 0.0; };
    LET $top = array::slice($retrieved, 0, $k);
    RETURN <float> array::len(array::intersect($top, $relevant)) / $k;
};

DEFINE FUNCTION OVERWRITE fn::reciprocal_rank($retrieved: array, $relevant: array) -> float {
    FOR $i IN 0..array::len($retrieved) {
        IF $retrieved[$i] IN $relevant { RETURN 1.0 / ($i + 1); };
    };
    RETURN 0.0;
};
