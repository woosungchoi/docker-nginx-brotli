##################################################
# Nginx with Brotli, Headers More modules.
##################################################

ARG ALPINE_IMAGE=alpine:3.23@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0
FROM ${ALPINE_IMAGE} AS builder

LABEL maintainer="Woosungchoi <https://github.com/woosungchoi>"

ENV NGINX_VERSION=1.30.5
ENV PCRE_VERSION=10.48
ENV ZLIB_VERSION=1.3.2
ENV NGINX_SHA256=6c20565aa2325cb82216ae804f4a4ff1875179014759a381c42ddc8e11c4906d
ENV PCRE_SHA256=ebcc25aadf2a51fa1fefa9b8bc9e7a79b3dae86870a0f1152a22e42befd46888
ENV ZLIB_SHA256=bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16
ENV BROTLI_COMMIT=a71f9312c2deb28875acc7bacfdd5695a111aa53
ENV HEADERS_MORE_COMMIT=04b13238d1d34f57d3232b7da65b89b93720854f
ENV COOKIE_FLAG_COMMIT=c4ff449318474fbbb4ba5f40cb67ccd54dc595d4
ARG BUILD_JOBS=2

RUN set -eux; \
  CONFIG="\
  --prefix=/etc/nginx \
  --sbin-path=/usr/sbin/nginx \
  --modules-path=/usr/lib/nginx/modules \
  --conf-path=/etc/nginx/nginx.conf \
  --error-log-path=/var/log/nginx/error.log \
  --http-log-path=/var/log/nginx/access.log \
  --pid-path=/var/run/nginx.pid \
  --lock-path=/var/run/nginx.lock \
  --http-client-body-temp-path=/var/cache/nginx/client_temp \
  --http-proxy-temp-path=/var/cache/nginx/proxy_temp \
  --http-fastcgi-temp-path=/var/cache/nginx/fastcgi_temp \
  --http-uwsgi-temp-path=/var/cache/nginx/uwsgi_temp \
  --http-scgi-temp-path=/var/cache/nginx/scgi_temp \
  --user=nginx \
  --group=nginx \
  --with-pcre=/usr/src/pcre2-${PCRE_VERSION} \
  --with-pcre-jit \
  --with-zlib=/usr/src/zlib-${ZLIB_VERSION} \
  --with-http_ssl_module \
  --with-http_realip_module \
  --with-http_addition_module \
  --with-http_sub_module \
  --with-http_dav_module \
  --with-http_flv_module \
  --with-http_mp4_module \
  --with-http_gunzip_module \
  --with-http_gzip_static_module \
  --with-http_random_index_module \
  --with-http_secure_link_module \
  --with-http_stub_status_module \
  --with-http_auth_request_module \
  --with-http_xslt_module=dynamic \
  --with-http_image_filter_module=dynamic \
  --with-http_geoip_module=dynamic \
  --with-http_perl_module=dynamic \
  --with-threads \
  --with-stream \
  --with-stream_ssl_module \
  --with-stream_ssl_preread_module \
  --with-stream_realip_module \
  --with-stream_geoip_module=dynamic \
  --with-http_slice_module \
  --with-mail \
  --with-mail_ssl_module \
  --with-compat \
  --with-file-aio \
  --with-http_v2_module \
  --with-http_v3_module \
  --with-compat --add-dynamic-module=/usr/src/ngx_brotli \
  --add-module=/usr/src/headers-more-nginx-module \
  --add-module=/usr/src/nginx_cookie_flag_module \
  --with-cc-opt=-Wno-error \
  " \
  && addgroup -S nginx \
  && adduser -D -S -h /var/cache/nginx -s /sbin/nologin -G nginx nginx \
  && apk add --no-cache ca-certificates \
  && update-ca-certificates \
  && apk add --no-cache --virtual .build-deps \
  gcc \
  libc-dev \
  make \
  openssl-dev \
  pcre-dev \
  zlib-dev \
  linux-headers \
  pax-utils \
  libxslt-dev \
  gd-dev \
  geoip-dev \
  perl-dev \
  && apk add --no-cache --virtual .brotli-build-deps \
  autoconf \
  libtool \
  automake \
  git \
  g++ \
  cmake \
  perl \
  patch \
  && mkdir -p /usr/src \
  && cd /usr/src \
  && wget -qO pcre.tar.gz https://github.com/PCRE2Project/pcre2/releases/download/pcre2-${PCRE_VERSION}/pcre2-${PCRE_VERSION}.tar.gz \
  && echo "$PCRE_SHA256  pcre.tar.gz" | sha256sum -c - \
  && tar zxf pcre.tar.gz && rm pcre.tar.gz \
  && wget -qO zlib.tar.gz https://github.com/madler/zlib/releases/download/v${ZLIB_VERSION}/zlib-${ZLIB_VERSION}.tar.gz \
  && echo "$ZLIB_SHA256  zlib.tar.gz" | sha256sum -c - \
  && tar zxf zlib.tar.gz && rm zlib.tar.gz \
  && wget -qO nginx.tar.gz https://nginx.org/download/nginx-$NGINX_VERSION.tar.gz \
  && echo "$NGINX_SHA256  nginx.tar.gz" | sha256sum -c - \
  && tar -zxC /usr/src -f nginx.tar.gz \
  && rm nginx.tar.gz \
  && git clone https://github.com/google/ngx_brotli \
  && git -C ngx_brotli checkout --detach "$BROTLI_COMMIT" \
  && git -C ngx_brotli submodule update --init --recursive \
  && cd ngx_brotli/deps/brotli \
  && mkdir out && cd out \
  && cmake -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DCMAKE_C_FLAGS="-Ofast -flto -funroll-loops -ffunction-sections -fdata-sections -Wl,--gc-sections" -DCMAKE_CXX_FLAGS="-Ofast -flto -funroll-loops -ffunction-sections -fdata-sections -Wl,--gc-sections" -DCMAKE_INSTALL_PREFIX=./installed .. \
  && cmake --build . --config Release --target brotlienc \
  && cd ../../../.. \
  && cd pcre2-${PCRE_VERSION} \
  && ./configure \
  && make \
  && make install \
  && cd .. \
  && cd zlib-${ZLIB_VERSION} \
  && ./configure \
  && make \
  && make install \
  && cd .. \
  && git clone https://github.com/openresty/headers-more-nginx-module \
  && git -C headers-more-nginx-module checkout --detach "$HEADERS_MORE_COMMIT" \
  && git clone https://github.com/AirisX/nginx_cookie_flag_module \
  && git -C nginx_cookie_flag_module checkout --detach "$COOKIE_FLAG_COMMIT" \
  && cd /usr/src/nginx-$NGINX_VERSION \
  && ./configure $CONFIG --with-debug --build="pcre-${PCRE_VERSION} zlib-${ZLIB_VERSION} headers-more-nginx-module-$(git --git-dir=/usr/src/headers-more-nginx-module/.git rev-parse --short HEAD) nginx_cookie_flag_module-$(git --git-dir=/usr/src/nginx_cookie_flag_module/.git rev-parse --short HEAD)" \
  && make -j"$BUILD_JOBS" \
  && mv objs/nginx objs/nginx-debug \
  && mv objs/ngx_http_xslt_filter_module.so objs/ngx_http_xslt_filter_module-debug.so \
  && mv objs/ngx_http_image_filter_module.so objs/ngx_http_image_filter_module-debug.so \
  && mv objs/ngx_http_geoip_module.so objs/ngx_http_geoip_module-debug.so \
  && mv objs/ngx_http_perl_module.so objs/ngx_http_perl_module-debug.so \
  && mv objs/ngx_stream_geoip_module.so objs/ngx_stream_geoip_module-debug.so \
  && ./configure $CONFIG --build="pcre-${PCRE_VERSION} zlib-${ZLIB_VERSION} headers-more-nginx-module-$(git --git-dir=/usr/src/headers-more-nginx-module/.git rev-parse --short HEAD) nginx_cookie_flag_module-$(git --git-dir=/usr/src/nginx_cookie_flag_module/.git rev-parse --short HEAD)" \
  && make -j"$BUILD_JOBS" \
  && make install \
  && rm -rf /etc/nginx/html/ \
  && mkdir /etc/nginx/conf.d/ \
  && mkdir -p /usr/share/nginx/html/ \
  && install -m644 html/index.html /usr/share/nginx/html/ \
  && install -m644 html/50x.html /usr/share/nginx/html/ \
  && install -m755 objs/nginx-debug /usr/sbin/nginx-debug \
  && install -m755 objs/ngx_http_xslt_filter_module-debug.so /usr/lib/nginx/modules/ngx_http_xslt_filter_module-debug.so \
  && install -m755 objs/ngx_http_image_filter_module-debug.so /usr/lib/nginx/modules/ngx_http_image_filter_module-debug.so \
  && install -m755 objs/ngx_http_geoip_module-debug.so /usr/lib/nginx/modules/ngx_http_geoip_module-debug.so \
  && install -m755 objs/ngx_http_perl_module-debug.so /usr/lib/nginx/modules/ngx_http_perl_module-debug.so \
  && install -m755 objs/ngx_stream_geoip_module-debug.so /usr/lib/nginx/modules/ngx_stream_geoip_module-debug.so \
  && ln -s ../../usr/lib/nginx/modules /etc/nginx/modules \
  && strip /usr/sbin/nginx* \
  && strip /usr/lib/nginx/modules/*.so \
  && rm -rf /usr/src/nginx-$NGINX_VERSION \
  && rm -rf /usr/src/ngx_brotli \
  && rm -rf /usr/src/headers-more-nginx-module \
  && rm -rf /usr/src/nginx_cookie_flag_module \
  \
  # Bring in gettext so we can get `envsubst`, then throw
  # the rest away. To do this, we need to install `gettext`
  # then move `envsubst` out of the way so `gettext` can
  # be deleted completely, then move `envsubst` back.
  && apk add --no-cache --virtual .gettext gettext \
  && mv /usr/bin/envsubst /tmp/ \
  \
  # Pass SONAME providers to the runtime stage; include both binaries and all modules.
  && mv /tmp/envsubst /usr/local/bin/

RUN set -eux; scanelf --needed --nobanner --format '%n#p' /usr/sbin/nginx /usr/sbin/nginx-debug /usr/lib/nginx/modules/*.so /usr/local/bin/envsubst \
  | tr ',' '\n' | sort -u | sed '/^$/d; s/^/so:/' > /tmp/nginx-rundeps

FROM ${ALPINE_IMAGE}

COPY --from=builder /usr/sbin/nginx /usr/sbin/nginx-debug /usr/sbin/
COPY --from=builder /usr/lib/nginx/modules/ /usr/lib/nginx/modules/
COPY --from=builder /usr/local/lib/perl5/ /usr/local/lib/perl5/
COPY --from=builder /usr/share/nginx/html/* /usr/share/nginx/html/
COPY --from=builder /etc/nginx/ /etc/nginx/
COPY --from=builder /usr/local/bin/envsubst /usr/local/bin/
COPY --from=builder /tmp/nginx-rundeps /tmp/nginx-rundeps
COPY default.nginx.conf /etc/nginx/nginx.conf

RUN \
  # Bring in tzdata so users could set the timezones through the environment
  # variables
  apk add --no-cache tzdata \
  \
  && apk add --no-cache \
  $(cat /tmp/nginx-rundeps) \
  && rm /tmp/nginx-rundeps \
  && apk info -vv > /usr/share/nginx/apk-runtime.txt \
  && addgroup -S nginx \
  && adduser -D -S -h /var/cache/nginx -s /sbin/nologin -G nginx nginx \
  # forward request and error logs to docker log collector
  && mkdir -p /var/cache/nginx /var/log/nginx \
  && touch /var/log/nginx/access.log /var/log/nginx/error.log \
  && chown nginx: /var/log/nginx/access.log /var/log/nginx/error.log \
  && ln -sf /dev/stdout /var/log/nginx/access.log \
  && ln -sf /dev/stderr /var/log/nginx/error.log

# Recommended nginx configuration. Please copy the config you wish to use.
# COPY nginx.conf /etc/nginx/
# COPY h3.nginx.conf /etc/nginx/conf.d/

EXPOSE 80 443/tcp 443/udp

STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]

# Build-time metadata as defined at http://label-schema.org
ARG BUILD_DATE
ARG VCS_REF

LABEL org.label-schema.build-date="$BUILD_DATE" \
  org.label-schema.vcs-ref="$VCS_REF" \
  org.label-schema.vcs-url="https://github.com/woosungchoi/docker-nginx-brotli.git"
