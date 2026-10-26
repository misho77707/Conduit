FROM node:20-slim AS build
WORKDIR /srv
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /srv/dist /usr/share/nginx/html
# nginx.conf is a template: when the container starts, the image fills in the ${CONDUIT_*} variables (only those).
ENV NGINX_ENVSUBST_FILTER=CONDUIT_ CONDUIT_UPLOAD_CEILING_MB=2048
COPY nginx.conf /etc/nginx/templates/default.conf.template
EXPOSE 80
