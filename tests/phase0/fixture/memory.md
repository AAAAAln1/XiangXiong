target: phase0.example.test
tech: nginx
port: 443 https
artifact: results/phase0.example.test/responses/req_001.txt
surface: /login form present
surface: /api/v1/search endpoint
cred: demo cookie session=phase0
vuln: reflected xss on /legacy (demo fixture only)
hypothesis: search q parameter may reflect unsanitized
hypothesis [observed]: admin panel may exist at /admin
hypothesis [blocked]: cannot verify auth bypass without credentials
hypothesis [confirmed]: /health returns 200 without auth
hypothesis [contradicted]: /debug was not open on first probe
counter: /old-admin returned 404 repeatedly
dead: /phpmyadmin not reachable
future: later recheck rate limits after creds
