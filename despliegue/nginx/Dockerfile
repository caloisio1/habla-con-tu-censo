# nginx de "Habla con tu Censo": proxy, límites de frecuencia y TLS opcional.
FROM nginx:1.28-alpine
RUN rm -f /etc/nginx/conf.d/default.conf
COPY conf.d/ /etc/nginx/conf.d/
COPY snippets/ /etc/nginx/snippets/
COPY plantillas/ /etc/nginx/plantillas/
