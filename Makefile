.PHONY: setup up down restart logs ps superuser test update-attendee sync-now shell stt-keys sabah-reader-sql backend-env

setup:            ## first install / re-run safely
	./scripts/bootstrap.sh

up:
	docker compose up -d

stt-keys:         ## after adding/changing GLADIA_API_KEY (etc.) in .env — stores it in Attendee
	./scripts/bootstrap.sh --keys-only

sabah-reader-sql: ## SQL for the read-only role on the SABAH.OS database (password from .env)
	@./scripts/sabah-reader-sql.sh

backend-env:      ## the MEETBOT_API_URL / MEETBOT_API_TOKEN lines for each SABAH.OS backend .env
	@./scripts/backend-env.sh

down:
	docker compose down

# Restarts only the bridge — never the bot workers, so meetings in progress are not cut.
restart:
	docker compose up -d --build meetbot-web meetbot-worker

logs:
	docker compose logs -f --tail=200 meetbot-worker meetbot-web attendee-worker-bot

ps:
	docker compose ps

superuser:        ## login for the Meetbot admin (/admin/)
	docker compose exec meetbot-web python manage.py createsuperuser

sync-now:         ## run calendar sync + summaries once, right now
	docker compose exec meetbot-worker python manage.py run_worker --once

shell:
	docker compose exec meetbot-web python manage.py shell

test:             ## bridge unit tests (sqlite, no external services)
	docker compose run --rm --no-deps -e MEETBOT_DEBUG=1 -e MEETBOT_TESTING=1 -e DATABASE_URL=sqlite:// \
		meetbot-web python manage.py test meetings

update-attendee:  ## after bumping ATTENDEE_REF in .env; restarts bot workers — do it outside meeting hours
	./scripts/fetch-attendee.sh
	docker compose build attendee-app
	docker compose up -d
