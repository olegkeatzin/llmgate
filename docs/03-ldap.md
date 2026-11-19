# LDAP / Active Directory

Портал проверяет пароль так: сервисной учёткой ищет пользователя, затем пробует
`bind` от его DN с введённым паролем. Пароль не сохраняется и не логируется.

## Что нужно от каталога

1. **Сервисный аккаунт** с правом чтения пользователей и групп (обычная доменная
   учётка без админских прав подходит).
2. **Группы доступа**, например `LLM-Users`, `LLM-Engineers`, `LLM-Admins`.
3. **Сетевой доступ** от контейнера `portal` до контроллера домена: 389 (LDAP)
   или 636 (LDAPS).

## Пример для Active Directory

```dotenv
LDAP_URI=ldap://dc01.corp.local:389
LDAP_START_TLS=true
LDAP_TLS_VERIFY=true

LDAP_BIND_DN=CN=svc-llmgate,OU=Service Accounts,DC=corp,DC=local
LDAP_BIND_PASSWORD=<пароль сервисной учётки>

LDAP_USER_BASE=OU=Employees,DC=corp,DC=local
LDAP_USER_FILTER=(&(objectClass=person)(sAMAccountName={username}))
LDAP_DISPLAY_NAME_ATTR=displayName
LDAP_EMAIL_ATTR=mail

LDAP_GROUP_ATTR=memberOf
LDAP_GROUP_BASE=OU=Groups,DC=corp,DC=local
LDAP_GROUP_FILTER=(&(objectClass=group)(member={dn}))
LDAP_NESTED_GROUPS=true
```

Замечания:

- Вместо DN сервисной учётки AD принимает UPN: `LDAP_BIND_DN=svc-llmgate@corp.local`.
- `LDAP_USER_FILTER` должен содержать `{username}` — портал экранирует значение,
  так что инъекция через логин невозможна.
- `LDAP_GROUP_ATTR=memberOf` — самый быстрый путь. Если в вашем домене `memberOf`
  не заполняется (бывает при нестандартной схеме), оставьте атрибут пустым:
  группы найдутся поиском по `LDAP_GROUP_FILTER`.
- `LDAP_NESTED_GROUPS=true` включает AD-правило `1.2.840.113556.1.4.1941`:
  сотрудник получит роль, если состоит во вложенной группе. Дороже по времени,
  поэтому по умолчанию выключено.

## AD: UPN, кириллица и база внутри OU

Если сотрудники входят по UPN (`ivanov@corp.local`), логины содержат кириллицу,
а корень домена отдаёт referrals:

```dotenv
LDAP_URI=ldap://dc01.corp.local:389
LDAP_START_TLS=true
# Пока сертификат контроллера самоподписанный/просрочен — шифруем, но не проверяем.
LDAP_TLS_VERIFY=false

LDAP_BIND_DN=svc-llmgate@corp.local
LDAP_BIND_PASSWORD=<пароль сервисной учётки>

# Важно: конкретное OU, где лежат люди и группы, а не DC=corp,DC=local.
LDAP_USER_BASE=OU=Company,DC=corp,DC=local
LDAP_USER_FILTER=(&(objectClass=person)(|(sAMAccountName={username})(userPrincipalName={username})))
LDAP_GROUP_BASE=OU=Company,DC=corp,DC=local
```

Почему фильтр с `|`: сотрудник может ввести и короткий логин, и полный UPN — портал
проверит оба атрибута. Дописывать `@corp.local` вручную не нужно: если ввели UPN,
сработает `userPrincipalName`, если короткий логин — `sAMAccountName`.

**База поиска — OU, а не корень домена.** От корня AD отдаёт referrals на
`CN=Configuration` и DNS-зоны, клиент падает с `Operations error`. Указывайте OU,
внутри которого лежат и пользователи, и группы.

Кириллица: `sAMAccountName` и `userPrincipalName` допускают Unicode. Портал
экранирует значение фильтра и отправляет его в UTF-8, поэтому `Лысенко_ОД`
и `Лысенко_ОД@corp.local` ищутся одинаково.

Проверка (до входа в браузер):

```bash
docker compose run --rm --no-deps portal python -m app.ldapcheck "Иванов_ИИ"
docker compose run --rm --no-deps portal python -m app.ldapcheck "Иванов_ИИ@corp.local"
```

Если `memberOf` у пользователя пустой (частая ситуация при нестандартной схеме),
группы найдутся вторым запросом по `LDAP_GROUP_FILTER` — для этого и нужен
`LDAP_GROUP_BASE`.

## Пример для OpenLDAP

```dotenv
LDAP_URI=ldaps://ldap.corp.local:636
LDAP_TLS_VERIFY=true
LDAP_BIND_DN=cn=svc-llmgate,ou=services,dc=corp,dc=local
LDAP_BIND_PASSWORD=<пароль>
LDAP_USER_BASE=ou=people,dc=corp,dc=local
LDAP_USER_FILTER=(&(objectClass=inetOrgPerson)(uid={username}))
LDAP_GROUP_ATTR=memberOf
LDAP_GROUP_BASE=ou=groups,dc=corp,dc=local
LDAP_GROUP_FILTER=(&(objectClass=groupOfNames)(member={dn}))
```

Для `posixGroup` (где членство в `memberUid`, а не в `member`) используйте
`{username}` в фильтре группы:

```dotenv
LDAP_GROUP_FILTER=(&(objectClass=posixGroup)(memberUid={username}))
```

## Как группы превращаются в роль

Портал нормализует значения групп к нижнему регистру и сравнивает и **полный DN**,
и **CN**: `CN=LLM-Engineers,OU=Groups,DC=corp,DC=local` совпадёт с `LLM-Engineers`
в `config/access.yaml`. Поэтому в политике можно писать короткие имена.

Если пользователь вошёл, но не попал ни в одну роль — он увидит страницу
«У вашей учётной записи нет доступа». Это не ошибка LDAP: не хватает записи в
`access.yaml` (или группы у сотрудника).

## Диагностика

Самый быстрый способ — скрипт внутри контейнера. Он проверяет всю цепочку:
bind сервисной учёткой → поиск → bind пользователем → группы → роль.

```bash
docker compose exec -it portal python -m app.ldapcheck ivanov
```

Пример вывода:

```
Пароль LDAP:
OK: ivanov — Иван Иванов
DN: CN=Иванов Иван,OU=Employees,DC=corp,DC=local
Группы:
  - cn=llm-engineers,ou=groups,dc=corp,dc=local
  - llm-engineers

Роль: engineer (Разработчик)
Модели: qwen3-32b, qwen3-8b, bge-m3
Срок ключа: 24 ч, ключей одновременно: 2, rpm: 120, tpm: 200000
```

Если нужно проверить каталог без портала:

```bash
# поиск пользователя
ldapsearch -x -H ldap://dc01.corp.local -D "CN=svc-llmgate,OU=Service Accounts,DC=corp,DC=local" \
  -W -b "OU=Employees,DC=corp,DC=local" "(sAMAccountName=ivanov)" dn displayName memberOf

# проверка пароля пользователя
ldapwhoami -x -H ldap://dc01.corp.local -D "CN=Иванов Иван,OU=Employees,DC=corp,DC=local" -W
```

Полезные сообщения в логах портала:

```bash
make logs | grep event=login
# event=login.ok user='ivanov' policy=engineer ...
# event=login.failed user='ivanov' ...
# event=login.denied user='ivanov' groups=[...]
```

## Типовые проблемы

| Симптом | Причина | Решение |
|---|---|---|
| «Сервер LDAP недоступен» | DNS/файрвол/порт | `docker compose exec portal getent hosts dc01.corp.local`; открыть 389/636 |
| «Неверный логин или пароль» у всех | сервисная учётка не биндится | проверить `LDAP_BIND_DN`/`LDAP_BIND_PASSWORD`, формат DN |
| Логин верный, но «нет доступа» | группы не найдены | `ldapcheck`, проверить `LDAP_GROUP_*` и `access.yaml` |
| `LDAPStartTLSError` | StartTLS не поддержан или сертификат | `LDAP_START_TLS=false` на 389 либо `ldaps://636` |
| `CERTIFICATE_VERIFY_FAILED` | самоподписанный сертификат DC | поставить корневой сертификат в контейнер или (осознанно) `LDAP_TLS_VERIFY=false` |
| Группы есть, роль не та | сработала другая роль выше по списку | порядок `policies` = приоритет |

## Безопасность

- StartTLS/LDAPS обязательны в проде: иначе пароль идёт открытым текстом.
- `LDAP_TLS_VERIFY=false` — только для отладки.
- Пароль сервисной учётки лежит в `.env` (права 600, файл в `.gitignore`).
- Подбор пароля ограничен: 5 неудач за 10 минут → блокировка на 15 минут
  (настраивается `PORTAL_LOGIN_*`). Счётчик в памяти портала.
- Портал не логирует пароли, DN пользователя в логи не пишется (только логин).
