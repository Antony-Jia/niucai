FROM node:22-alpine AS build
WORKDIR /repo
COPY apps/desktop/package*.json apps/desktop/
RUN npm ci --prefix apps/desktop
COPY apps/web/package*.json apps/web/
RUN npm ci --prefix apps/web
COPY apps/desktop/src apps/desktop/src
COPY apps/web apps/web
RUN npm run build --prefix apps/web
FROM nginx:1.28-alpine
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /repo/apps/web/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
