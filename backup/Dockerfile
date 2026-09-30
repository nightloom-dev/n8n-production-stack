# pg_dump, pg_restore and a scratch server, same major version as the db, plus restic
FROM postgres:17.11-alpine
RUN apk add --no-cache restic curl tzdata
COPY backup.sh /usr/local/bin/backup.sh
ENTRYPOINT ["backup.sh"]
CMD ["schedule"]
