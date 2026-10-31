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
