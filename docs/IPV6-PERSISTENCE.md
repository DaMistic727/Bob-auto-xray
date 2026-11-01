# IPv6 persistence · v0.3.5 Preview

## Доказанное поведение и вероятная причина

Linux `all.disable_ipv6=1` меняет текущие интерфейсы, `default` задаёт начальное значение новых. Это не запрет последующих записей другим процессом. Возврат per-interface `disable_ipv6` в 0 способен создать link-local адрес ([kernel IP sysctl](https://docs.kernel.org/networking/ip-sysctl.html)).

В systemd v259 `link_update_ipv6_sysctl()` пишет `disable_ipv6=false`, если `link_ipv6_enabled()` видит IPv6 link-local или статическую IPv6-конфигурацию. Поэтому networkd, настраивающий интерфейс после раннего systemd-sysctl или при повторной инициализации, является вероятным источником наблюдавшейся записи. Источники: [networkd-sysctl.c](https://github.com/systemd/systemd/blob/v259/src/network/networkd-sysctl.c), [networkd-link.c](https://github.com/systemd/systemd/blob/v259/src/network/networkd-link.c), [systemd.network](https://www.freedesktop.org/software/systemd/man/259/systemd.network.html).

Но старый проектный drop-in уже задавал `LinkLocalAddressing=no` и `IPv6AcceptRA=no`. По коду systemd эти параметры должны предотвращать обычную автоконфигурацию IPv6. В доступных исторических данных нет достаточного доказательства того, кто именно вернул ens3 в 0, какой effective networkd config действовал в этот момент и не было ли поздней регенерации Netplan. **Точная корневая причина исторического P1 не доказана, устранение после reboot не заявляется.**

## Минимальное усиление

v0.3.5 дополнительно задаёт `IPv6LinkLocalAddressGenerationMode=none` в том же принадлежащем Bob drop-in. Это документированный запрет генерации link-local; systemd v257/v259 также очищает IPv6LL flag при `none`. Оригинальные Netplan/.network файлы, IPv4 DHCP, адреса, DNS и маршруты не изменяются. Нет `netplan apply`, `networkctl reconfigure`, down/up, restart networkd, изменения bootloader или WARP.

Preflight отвергает чужие назначения address-generation в drop-in, как ранее отвергал LinkLocalAddressing/IPv6AcceptRA. Только точный прежний принадлежащий Bob файл допускается к расширению; изменённый файл отклоняется. Snapshot/rollback сохраняют оба файла и runtime sysctl. После `networkctl reload` параметры применяются к существующим интерфейсам. Verify проверяет все значения и **все** IPv6-адреса, включая link-local; глобальный фильтр больше не может скрыть остаточный адрес.

Mock regression воспроизводит последовательность «успешное применение → повторная инициализация ens3 с 0 → обязательный FAIL», затем разрешённое повторное применение с восстановлением политики; отдельно проверены link-local при формально правильном sysctl, legacy rollback и чужой address-generation drop-in. Модель проверяет обнаружение и восстановление, а не доказывает поведение реального networkd при загрузке. Существующие завершённые установки не переустанавливаются и не мигрируют автоматически.

## Ручная проверка владельцем после публикации

До и после reboot сохранить `networkctl status --no-pager --full ens3`, active Netplan `.network` и его drop-in, `sysctl net.ipv6.conf.all.disable_ipv6 net.ipv6.conf.default.disable_ipv6 net.ipv6.conf.ens3.disable_ipv6`, `ip -6 address show`, `journalctl -b -u systemd-networkd -u systemd-sysctl`. Ожидаются 1 для all/default/каждого интерфейса и отсутствие IPv6-адресов. При 0 или link-local считать persistence FAIL и сохранить effective config/журнал, не объявлять систему готовой к production. Проверить сохранность IPv4 SSH/DNS/REALITY/WARP.

Чистая установка, повторная инициализация настоящего интерфейса и reboot v0.3.5: **NOT TESTED**. VPS при подготовке релиза не использовались.

## v0.4.0: причина доказана и исправлена (1 ноября 2026)

**Наблюдение до исправления.** На всех нодах E2E Doctor и на чистой установке кандидата v0.4.0 (Ubuntu 26.04.1, systemd 259, netplan → networkd, `ens3`) до reboot IPv6 выключен полностью, после reboot `ens3` имеет `disable_ipv6=0` и один link-local адрес; глобального IPv6 и IPv6-маршрутов нет.

**Доказательство.** С `SYSTEMD_LOG_LEVEL=debug` для systemd-networkd журнал загрузки содержит:

```
systemd-networkd: Failed to chase and open directory '/etc/systemd/network/10-netplan-ens3.network.d', ignoring: Permission denied
systemd-networkd: ens3: Configuring with /run/systemd/network/10-netplan-ens3.network.
systemd-networkd: Setting '/proc/sys/net/ipv6/conf/ens3/disable_ipv6' to '0'
systemd-networkd: ens3: Gained IPv6LL
```

Каталог drop-in имел права `0700`: установщик работает с `umask 077`, а `atomic_write()` создавал отсутствующий каталог с этими правами. systemd-networkd работает от `systemd-network`, не видел `LinkLocalAddressing=no` и по netplan-файлу (`LinkLocalAddressing=ipv6`) включал IPv6 (`link_update_ipv6_sysctl()` в systemd v259). До reboot это скрывали прямые записи sysctl установщика; `networkctl cat` запускается от root и показывал drop-in, что вводило в заблуждение. Ранняя гипотеза о networkd в initramfs не подтвердилась: initrd кратко поднимает link-local по `zzzz-dracut-default.network`, но после switch-root `systemd-sysctl` его снимает.

**Исправление v0.4.0.** `ipv6.configure()` создаёт каталог drop-in с правами `0755` (существующий каталог Bob получает `0755`); `ipv6.verify()` отказывает, если networkd не может прочитать drop-in; Doctor показывает такой drop-in как `WARN` с командой `chmod 755 <каталог>`.

**Проверка на VPS.** Doctor кандидата указал проблему на ноде, установленной до исправления. Применение политики кодом исправленной сборки под `umask 077` дало каталог `755`, networkd сразу перешёл на `IPv6 Address Generation Mode: none`. После reboot: `all/default/lo/ens3 disable_ipv6 = 1`, IPv6-адресов 0, маршрутов 0, networkd пишет `Configuring with … (dropins: …99-zz-bob-auto-xray-ipv4-only.conf)` и `disable_ipv6` не меняет; Doctor `HEALTHY`. Подробности: [E2E-V040.md](E2E-V040.md).

**Уже установленные ноды** (v0.3.4–v0.3.8) автоматически не мигрируют. Достаточно `chmod 755 /etc/systemd/network/<netplan>.network.d` (точный путь показывает Doctor); вернуть текущую загрузку без reboot можно `sysctl -w net.ipv6.conf.<интерфейс>.disable_ipv6=1`.
