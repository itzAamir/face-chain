FROM node:22-bookworm-slim

WORKDIR /workspace

COPY package.json .
RUN npm install --no-audit --no-fund

COPY . .

RUN mkdir -p /deployment \
    && chown -R node:node /workspace /deployment
USER node

EXPOSE 8545

CMD ["npm", "run", "node"]
