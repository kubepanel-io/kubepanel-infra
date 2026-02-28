FROM ubuntu:20.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    imagemagick \
    php-redis \
    php-imagick \
    php-fpm \
    php-mysql \
    php-curl \
    php-gd \
    php-mbstring \
    php-xml \
    php-xmlrpc \
    php-soap \
    php-intl \
    php-zip \
    sendmail \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 7777 webgroup && \
    useradd -u 7777 -g webgroup -m webuser
RUN ln -sf /proc/1/fd/1 /var/log/php7.4-fpm.log
COPY www.conf /etc/php/7.4/fpm/pool.d/www.conf
COPY php-fpm.conf /etc/php/7.4/fpm/php-fpm.conf
COPY php.ini /etc/php/7.4/fpm/php.ini
# Create version-agnostic config path for KubePanel
RUN mkdir -p /etc/php-custom && \
    ln -sf /etc/php-custom/kubepanel.ini /etc/php/7.4/fpm/conf.d/99-kubepanel.ini
RUN chown -R webuser:webgroup /var/run /run /etc/php/7.4/fpm/pool.d /var/run/php
USER webuser
EXPOSE 9001
CMD ["sh", "-c", "php-fpm7.4 -F;"]

