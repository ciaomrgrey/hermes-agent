-- Read-only recovery: emit only event IDs and outer exception classes, never log text.
-- Reproduce: sqlite3 -readonly -header -csv <gate.db> < recover-errors.sql
WITH logs AS (
 SELECT p.column1 profile, CAST(readfile('/Users/claudia/hermes/home/profiles/' || p.column1 || '/logs/agent.log' || n.column1) AS TEXT) body
 FROM (VALUES ('cody'),('generalist'),('gurney'),('sophia'),('chas'),('plutus'),('jared'),('emma'),('ripley')) p
 CROSS JOIN (VALUES (''),('.1'),('.2'),('.3')) n
), candidates AS (
 SELECT e.id,e.profile, substr(l.body,instr(l.body,strftime('%Y-%m-%d %H:%M:%S', e.created,'unixepoch','localtime'))) tail
 FROM events e JOIN logs l ON e.profile=l.profile
 WHERE e.action='gate_error' AND e.diagnostics='{}'
 AND e.created>=1789727400-86400 AND e.created<1789727400
 AND instr(l.body,strftime('%Y-%m-%d %H:%M:%S', e.created,'unixepoch','localtime'))>0
), lines AS (
 SELECT id,profile,substr(tail,1,instr(tail,char(10))) line FROM candidates
)
SELECT id,profile,CASE
 WHEN instr(line,'Completion gate failed open (TimeoutExpired)')>0 THEN 'outer_timeout'
 WHEN instr(line,'Completion gate failed open (CalledProcessError)')>0 THEN 'child_exit_cause_unrecorded'
 ELSE 'unresolved_timestamp_match' END evidence
FROM lines ORDER BY id;
