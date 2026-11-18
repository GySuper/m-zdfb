/** Tailwind 只在开发机编译:tools/tailwindcss -c tailwind.config.js -i wxsp/static/app.src.css -o wxsp/static/app.css --minify */
module.exports = {
  content: ["./wxsp/templates/**/*.html", "./wxsp/static/**/*.js"],
  theme: {
    extend: {
      fontFamily: { sans: ['"MiSans"', "system-ui", "-apple-system", '"PingFang SC"', '"Microsoft YaHei"', "sans-serif"] },
    },
  },
  plugins: [],
};
